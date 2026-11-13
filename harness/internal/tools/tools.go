// Package tools reúne as ferramentas que o modelo pode pedir ao harness. Na Fase 1 são só de leitura
// (read_file, list_dir) e confinadas a uma pasta raiz: o modelo decide os caminhos, então todo caminho
// é resolvido (inclusive symlinks) e precisa ficar dentro da raiz.
package tools

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/aquele-dinho/tio-harness/harness/internal/llm"
)

const maxListEntries = 200

// Tool é uma ferramenta: definição para o modelo + execução.
type Tool struct {
	Name        string
	Description string
	Schema      json.RawMessage // JSON Schema dos argumentos
	Run         func(args json.RawMessage) (string, error)
}

// Registry guarda as ferramentas disponíveis e a raiz do sandbox.
type Registry struct {
	root     string
	maxBytes int
	tools    map[string]Tool
	order    []string
}

// NewRegistry cria o registro com a raiz resolvida (absoluta, sem symlinks). maxBytes limita cada leitura.
func NewRegistry(root string, maxBytes int) (*Registry, error) {
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	real, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return nil, fmt.Errorf("raiz inválida %q: %w", root, err)
	}
	if st, err := os.Stat(real); err != nil || !st.IsDir() {
		return nil, fmt.Errorf("a raiz %q não é uma pasta", root)
	}
	if maxBytes <= 0 {
		maxBytes = 16 * 1024
	}
	r := &Registry{root: real, maxBytes: maxBytes, tools: map[string]Tool{}}
	r.add(Tool{
		Name:        "read_file",
		Description: "Lê um arquivo de texto do projeto e devolve o conteúdo. O caminho é relativo à raiz do projeto.",
		Schema:      json.RawMessage(`{"type":"object","properties":{"path":{"type":"string","description":"caminho do arquivo, relativo à raiz do projeto"}},"required":["path"]}`),
		Run:         r.readFile,
	})
	r.add(Tool{
		Name:        "list_dir",
		Description: "Lista os arquivos e pastas de um diretório do projeto (pastas terminam com /). O caminho é relativo à raiz; vazio ou \".\" é a raiz.",
		Schema:      json.RawMessage(`{"type":"object","properties":{"path":{"type":"string","description":"diretório relativo à raiz do projeto (padrão: .)"}}}`),
		Run:         r.listDir,
	})
	return r, nil
}

func (r *Registry) add(t Tool) {
	r.tools[t.Name] = t
	r.order = append(r.order, t.Name)
}

// Root devolve a raiz resolvida do sandbox.
func (r *Registry) Root() string { return r.root }

// Names lista os nomes das ferramentas, na ordem de registro.
func (r *Registry) Names() []string { return append([]string(nil), r.order...) }

// Describe devolve nome e descrição de uma ferramenta.
func (r *Registry) Describe(name string) (string, bool) {
	t, ok := r.tools[name]
	return t.Description, ok
}

// Defs devolve as definições no formato que o cliente LLM envia ao modelo.
func (r *Registry) Defs() []llm.ToolDef {
	defs := make([]llm.ToolDef, 0, len(r.order))
	for _, n := range r.order {
		t := r.tools[n]
		defs = append(defs, llm.ToolDef{Type: "function", Function: llm.FunctionDef{
			Name: t.Name, Description: t.Description, Parameters: t.Schema}})
	}
	return defs
}

// Call executa a ferramenta `name` com os argumentos JSON. Erros voltam como texto para o modelo se corrigir.
func (r *Registry) Call(name string, args json.RawMessage) (string, error) {
	t, ok := r.tools[name]
	if !ok {
		return "", fmt.Errorf("ferramenta desconhecida %q (disponíveis: %s)", name, strings.Join(r.order, ", "))
	}
	if len(args) == 0 {
		args = json.RawMessage(`{}`)
	}
	return t.Run(args)
}

// resolve transforma um caminho pedido pelo modelo em um caminho real DENTRO da raiz, ou falha.
func (r *Registry) resolve(p string) (string, error) {
	p = strings.TrimSpace(p)
	if p == "" {
		p = "."
	}
	candidate := p
	if !filepath.IsAbs(candidate) {
		candidate = filepath.Join(r.root, candidate)
	}
	candidate = filepath.Clean(candidate)
	outside := fmt.Errorf("acesso negado: %q fica fora da raiz do projeto", p)
	real, err := filepath.EvalSymlinks(candidate)
	if err != nil {
		// Fora da raiz, "não existe" e "existe mas é proibido" têm que parecer iguais: senão o modelo
		// consegue sondar quais arquivos existem fora do sandbox.
		if !within(r.root, candidate) {
			return "", outside
		}
		if errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("não existe: %s", p)
		}
		return "", err
	}
	if !within(r.root, real) { // inclui symlinks que apontam para fora
		return "", outside
	}
	return real, nil
}

// within diz se p está dentro de root (ou é o próprio root).
func within(root, p string) bool {
	rel, err := filepath.Rel(root, p)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

type pathArgs struct {
	Path string `json:"path"`
}

func (r *Registry) readFile(raw json.RawMessage) (string, error) {
	var a pathArgs
	if err := json.Unmarshal(raw, &a); err != nil {
		return "", fmt.Errorf("argumentos inválidos (esperado {\"path\": \"...\"}): %w", err)
	}
	if strings.TrimSpace(a.Path) == "" {
		return "", errors.New("falta o argumento path")
	}
	real, err := r.resolve(a.Path)
	if err != nil {
		return "", err
	}
	st, err := os.Stat(real)
	if err != nil {
		return "", err
	}
	if st.IsDir() {
		return "", fmt.Errorf("%s é uma pasta; use list_dir", a.Path)
	}
	f, err := os.Open(real)
	if err != nil {
		return "", err
	}
	defer f.Close()
	buf := make([]byte, r.maxBytes+1)
	n, err := io.ReadFull(f, buf)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return "", err
	}
	data := buf[:n]
	head := data
	if len(head) > 8000 {
		head = head[:8000]
	}
	if bytes.IndexByte(head, 0) >= 0 {
		return "", fmt.Errorf("%s parece binário; só leio texto", a.Path)
	}
	if n > r.maxBytes {
		return string(data[:r.maxBytes]) + fmt.Sprintf("\n[truncado: o arquivo tem %d bytes, mostrei os primeiros %d]", st.Size(), r.maxBytes), nil
	}
	return string(data), nil
}

func (r *Registry) listDir(raw json.RawMessage) (string, error) {
	var a pathArgs
	if err := json.Unmarshal(raw, &a); err != nil {
		return "", fmt.Errorf("argumentos inválidos (esperado {\"path\": \"...\"}): %w", err)
	}
	real, err := r.resolve(a.Path)
	if err != nil {
		return "", err
	}
	entries, err := os.ReadDir(real)
	if err != nil {
		return "", err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		n := e.Name()
		if e.IsDir() {
			n += "/"
		}
		names = append(names, n)
	}
	sort.Strings(names)
	extra := 0
	if len(names) > maxListEntries {
		extra = len(names) - maxListEntries
		names = names[:maxListEntries]
	}
	out := strings.Join(names, "\n")
	if out == "" {
		out = "(pasta vazia)"
	}
	if extra > 0 {
		out += fmt.Sprintf("\n[+%d entradas omitidas]", extra)
	}
	return out, nil
}
