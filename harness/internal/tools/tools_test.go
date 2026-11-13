package tools

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// layout cria: <tmp>/segredo.txt (FORA da raiz) e <tmp>/proj/{a.txt, sub/b.txt, bin.dat, grande.txt}.
func layout(t *testing.T) (proj string, outside string) {
	t.Helper()
	tmp := t.TempDir()
	proj = filepath.Join(tmp, "proj")
	outside = filepath.Join(tmp, "segredo.txt")
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.MkdirAll(filepath.Join(proj, "sub"), 0o755))
	must(os.WriteFile(outside, []byte("SEGREDO"), 0o644))
	must(os.WriteFile(filepath.Join(proj, "a.txt"), []byte("olá, harness\n"), 0o644))
	must(os.WriteFile(filepath.Join(proj, "sub", "b.txt"), []byte("dentro de sub"), 0o644))
	must(os.WriteFile(filepath.Join(proj, "bin.dat"), []byte{0x00, 0x01, 0x02}, 0o644))
	must(os.WriteFile(filepath.Join(proj, "grande.txt"), []byte(strings.Repeat("x", 100)), 0o644))
	return proj, outside
}

func call(t *testing.T, r *Registry, name, path string) (string, error) {
	t.Helper()
	args, _ := json.Marshal(map[string]string{"path": path})
	return r.Call(name, args)
}

func TestReadFile(t *testing.T) {
	proj, _ := layout(t)
	r, err := NewRegistry(proj, 50)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := call(t, r, "read_file", "a.txt"); err != nil || got != "olá, harness\n" {
		t.Fatalf("a.txt: %q, %v", got, err)
	}
	if got, err := call(t, r, "read_file", "sub/b.txt"); err != nil || got != "dentro de sub" {
		t.Fatalf("sub/b.txt: %q, %v", got, err)
	}
	// caminho absoluto DENTRO da raiz é aceito
	if got, err := call(t, r, "read_file", filepath.Join(r.Root(), "a.txt")); err != nil || !strings.HasPrefix(got, "olá") {
		t.Fatalf("absoluto dentro: %q, %v", got, err)
	}
}

func TestReadFileTruncates(t *testing.T) {
	proj, _ := layout(t)
	r, _ := NewRegistry(proj, 50)
	got, err := call(t, r, "read_file", "grande.txt")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got, strings.Repeat("x", 50)+"\n[truncado") || !strings.Contains(got, "100 bytes") {
		t.Fatalf("truncamento inesperado: %q", got)
	}
}

func TestReadFileErrors(t *testing.T) {
	proj, _ := layout(t)
	r, _ := NewRegistry(proj, 50)
	cases := []struct{ name, path, want string }{
		{"inexistente", "nao-existe.txt", "não existe"},
		{"pasta", "sub", "list_dir"},
		{"binario", "bin.dat", "binário"},
		{"vazio", "", "falta o argumento path"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := call(t, r, "read_file", c.path)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("esperava erro com %q, veio %v", c.want, err)
			}
		})
	}
}

func TestSandboxEscapes(t *testing.T) {
	proj, outside := layout(t)
	r, _ := NewRegistry(proj, 1024)

	// Caminhos que existem fora da raiz E que não existem: a resposta tem de ser a mesma (sem oráculo de existência).
	for _, p := range []string{"../segredo.txt", "../nao-existe-fora.txt", "sub/../../segredo.txt", "../../etc/passwd",
		outside, filepath.Join(filepath.Dir(outside), "nao-existe-fora.txt"), "..", "../.."} {
		for _, tool := range []string{"read_file", "list_dir"} {
			out, err := call(t, r, tool, p)
			if err == nil || !strings.Contains(err.Error(), "fora da raiz") {
				t.Fatalf("%s(%q) deveria negar acesso; out=%q err=%v", tool, p, out, err)
			}
			if strings.Contains(out, "SEGREDO") {
				t.Fatalf("%s(%q) vazou o segredo", tool, p)
			}
		}
	}

	// symlink DENTRO da raiz apontando para FORA da raiz
	link := filepath.Join(proj, "atalho.txt")
	if err := os.Symlink(outside, link); err != nil {
		t.Skipf("sem suporte a symlink neste ambiente: %v", err)
	}
	if out, err := call(t, r, "read_file", "atalho.txt"); err == nil || strings.Contains(out, "SEGREDO") {
		t.Fatalf("symlink para fora deveria ser negado; out=%q err=%v", out, err)
	}
	dirLink := filepath.Join(proj, "atalho-dir")
	if err := os.Symlink(filepath.Dir(outside), dirLink); err == nil {
		if out, err := call(t, r, "read_file", "atalho-dir/segredo.txt"); err == nil || strings.Contains(out, "SEGREDO") {
			t.Fatalf("symlink de pasta para fora deveria ser negado; out=%q err=%v", out, err)
		}
	}
	// symlink dentro da raiz apontando para dentro: permitido
	ok := filepath.Join(proj, "atalho-ok.txt")
	if err := os.Symlink(filepath.Join(proj, "a.txt"), ok); err == nil {
		if got, err := call(t, r, "read_file", "atalho-ok.txt"); err != nil || !strings.HasPrefix(got, "olá") {
			t.Fatalf("symlink interno deveria funcionar: %q, %v", got, err)
		}
	}
}

func TestListDir(t *testing.T) {
	proj, _ := layout(t)
	r, _ := NewRegistry(proj, 1024)
	got, err := call(t, r, "list_dir", "")
	if err != nil {
		t.Fatal(err)
	}
	want := "a.txt\nbin.dat\ngrande.txt\nsub/"
	if got != want {
		t.Fatalf("list_dir raiz:\n got %q\nwant %q", got, want)
	}
	if got, err := call(t, r, "list_dir", "sub"); err != nil || got != "b.txt" {
		t.Fatalf("list_dir sub: %q, %v", got, err)
	}
	// argumentos vazios {} valem como raiz
	if got, err := r.Call("list_dir", nil); err != nil || got != want {
		t.Fatalf("list_dir sem args: %q, %v", got, err)
	}
}

func TestRegistryBasics(t *testing.T) {
	proj, _ := layout(t)
	r, _ := NewRegistry(proj, 0)
	if _, err := r.Call("rm_rf", json.RawMessage(`{}`)); err == nil || !strings.Contains(err.Error(), "desconhecida") {
		t.Fatalf("ferramenta desconhecida deveria falhar: %v", err)
	}
	if _, err := r.Call("read_file", json.RawMessage(`não é json`)); err == nil || !strings.Contains(err.Error(), "inválidos") {
		t.Fatalf("argumentos inválidos deveriam falhar: %v", err)
	}
	defs := r.Defs()
	if len(defs) != 2 || defs[0].Function.Name != "read_file" || defs[1].Function.Name != "list_dir" {
		t.Fatalf("defs inesperadas: %+v", defs)
	}
	for _, d := range defs {
		if !json.Valid(d.Function.Parameters) {
			t.Fatalf("schema de %s não é JSON válido", d.Function.Name)
		}
	}
	if _, err := NewRegistry(filepath.Join(proj, "a.txt"), 0); err == nil {
		t.Fatal("raiz que é arquivo deveria falhar")
	}
	if _, err := NewRegistry(filepath.Join(proj, "nao-existe"), 0); err == nil {
		t.Fatal("raiz inexistente deveria falhar")
	}
}
