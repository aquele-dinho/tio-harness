package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aquele-dinho/tio-harness/harness/internal/llm"
	"github.com/aquele-dinho/tio-harness/harness/internal/tools"
)

// fakeLLM devolve respostas roteirizadas e guarda o que recebeu a cada chamada.
type fakeLLM struct {
	replies []llm.Message
	seen    [][]llm.Message
}

func (f *fakeLLM) Chat(_ context.Context, msgs []llm.Message, defs []llm.ToolDef, _ int) (llm.Message, error) {
	f.seen = append(f.seen, append([]llm.Message(nil), msgs...))
	if len(f.seen) > len(f.replies) {
		// esgotou o roteiro: o modelo "insiste" em pedir ferramenta
		return llm.Message{Role: "assistant", ToolCalls: []llm.ToolCall{call("loop", "list_dir", `{"path":"."}`)}}, nil
	}
	return f.replies[len(f.seen)-1], nil
}

func call(id, name, args string) llm.ToolCall {
	return llm.ToolCall{ID: id, Type: "function", Function: llm.FunctionCall{Name: name, Arguments: args}}
}

func setup(t *testing.T) *tools.Registry {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "main.go"), []byte("package main // ola"), 0o644); err != nil {
		t.Fatal(err)
	}
	reg, err := tools.NewRegistry(dir, 1024)
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

func last(msgs []llm.Message) llm.Message { return msgs[len(msgs)-1] }

func TestNativeToolCallThenAnswer(t *testing.T) {
	f := &fakeLLM{replies: []llm.Message{
		{Role: "assistant", ToolCalls: []llm.ToolCall{call("c1", "read_file", `{"path":"main.go"}`)}},
		{Role: "assistant", Content: "  É o pacote main.  "},
	}}
	var trace bytes.Buffer
	a := &Agent{LLM: f, Tools: setup(t), Trace: &trace}
	got, err := a.Run(context.Background(), "o que é main.go?")
	if err != nil {
		t.Fatal(err)
	}
	if got != "É o pacote main." {
		t.Fatalf("resposta final: %q", got)
	}
	// 2ª chamada ao modelo deve ter o resultado da ferramenta com o ID certo
	if len(f.seen) != 2 {
		t.Fatalf("esperava 2 chamadas ao modelo, houve %d", len(f.seen))
	}
	tr := last(f.seen[1])
	if tr.Role != "tool" || tr.ToolCallID != "c1" || tr.Content != "package main // ola" {
		t.Fatalf("mensagem de ferramenta incorreta: %+v", tr)
	}
	if f.seen[0][0].Role != "system" || !strings.Contains(f.seen[0][0].Content, "Never invent") {
		t.Fatalf("prompt de sistema padrão ausente: %+v", f.seen[0][0])
	}
	if !strings.Contains(trace.String(), "→ read_file") || !strings.Contains(trace.String(), "✓") {
		t.Fatalf("rastro inesperado: %q", trace.String())
	}
}

func TestBlueprintJSONBlockFallback(t *testing.T) {
	f := &fakeLLM{replies: []llm.Message{
		{Role: "assistant", Content: "Vou ler:\n```json\n{\"tool\": \"read_file\", \"path\": \"main.go\"}\n```"},
		{Role: "assistant", Content: "Achei: package main."},
	}}
	a := &Agent{LLM: f, Tools: setup(t)}
	got, err := a.Run(context.Background(), "leia main.go")
	if err != nil || got != "Achei: package main." {
		t.Fatalf("got %q, err %v", got, err)
	}
	fb := last(f.seen[1])
	if fb.Role != "user" || !strings.Contains(fb.Content, "Tool result for read_file") || !strings.Contains(fb.Content, "package main // ola") {
		t.Fatalf("resultado do fallback não foi devolvido ao modelo: %+v", fb)
	}
}

func TestToolErrorGoesBackToModel(t *testing.T) {
	f := &fakeLLM{replies: []llm.Message{
		{Role: "assistant", ToolCalls: []llm.ToolCall{call("c1", "read_file", `{"path":"../../etc/passwd"}`)}},
		{Role: "assistant", Content: "Não tenho acesso a esse arquivo."},
	}}
	a := &Agent{LLM: f, Tools: setup(t)}
	got, err := a.Run(context.Background(), "leia /etc/passwd")
	if err != nil || !strings.Contains(got, "Não tenho acesso") {
		t.Fatalf("got %q, err %v", got, err)
	}
	tr := last(f.seen[1])
	if tr.Role != "tool" || !strings.HasPrefix(tr.Content, "ERROR:") || !strings.Contains(tr.Content, "fora da raiz") {
		t.Fatalf("erro deveria voltar ao modelo como texto: %+v", tr)
	}
}

func TestUnknownToolAndBadArgs(t *testing.T) {
	f := &fakeLLM{replies: []llm.Message{
		{Role: "assistant", ToolCalls: []llm.ToolCall{call("c1", "delete_everything", `{}`), call("c2", "read_file", `{quebrado`)}},
		{Role: "assistant", Content: "ok"},
	}}
	a := &Agent{LLM: f, Tools: setup(t)}
	if _, err := a.Run(context.Background(), "x"); err != nil {
		t.Fatal(err)
	}
	msgs := f.seen[1]
	var errs []string
	for _, m := range msgs {
		if m.Role == "tool" {
			errs = append(errs, m.Content)
		}
	}
	if len(errs) != 2 || !strings.Contains(errs[0], "desconhecida") || !strings.HasPrefix(errs[1], "ERROR:") {
		t.Fatalf("erros das duas chamadas deveriam voltar: %q", errs)
	}
}

func TestMaxStepsExceeded(t *testing.T) {
	f := &fakeLLM{} // sempre pede ferramenta
	a := &Agent{LLM: f, Tools: setup(t), MaxSteps: 3}
	_, err := a.Run(context.Background(), "x")
	if err == nil || !strings.Contains(err.Error(), "3 passos") {
		t.Fatalf("esperava erro de limite de passos, veio %v", err)
	}
	if len(f.seen) != 3 {
		t.Fatalf("esperava exatamente 3 chamadas, houve %d", len(f.seen))
	}
}

func TestCustomSystemPrompt(t *testing.T) {
	f := &fakeLLM{replies: []llm.Message{{Role: "assistant", Content: "oi"}}}
	a := &Agent{LLM: f, Tools: setup(t), System: "SEJA BREVE"}
	if _, err := a.Run(context.Background(), "x"); err != nil {
		t.Fatal(err)
	}
	if f.seen[0][0].Content != "SEJA BREVE" {
		t.Fatalf("system personalizado ignorado: %q", f.seen[0][0].Content)
	}
}

func TestParseToolBlock(t *testing.T) {
	cases := []struct {
		name, in, tool, args string
		ok                   bool
	}{
		{"puro", `{"tool":"read_file","path":"main.go"}`, "read_file", `{"path":"main.go"}`, true},
		{"cerca", "```json\n{\"tool\": \"list_dir\", \"path\": \"src\"}\n```", "list_dir", `{"path":"src"}`, true},
		{"texto antes", `Preciso disso: {"tool":"read_file","path":"a.go"} obrigado`, "read_file", `{"path":"a.go"}`, true},
		{"arguments aninhado", `{"tool":"read_file","arguments":{"path":"b.go"}}`, "read_file", `{"path":"b.go"}`, true},
		{"chaves soltas antes", `{ não é json } {"tool":"read_file","path":"c.go"}`, "read_file", `{"path":"c.go"}`, true},
		{"sem tool", `{"path":"main.go"}`, "", "", false},
		{"texto comum", "A resposta é 42.", "", "", false},
		{"tool vazio", `{"tool":""}`, "", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			name, args, ok := ParseToolBlock(c.in)
			if ok != c.ok || name != c.tool {
				t.Fatalf("got (%q, %s, %v), want (%q, %s, %v)", name, args, ok, c.tool, c.args, c.ok)
			}
			if ok {
				var got, want map[string]any
				_ = json.Unmarshal(args, &got)
				_ = json.Unmarshal([]byte(c.args), &want)
				if len(got) != len(want) || got["path"] != want["path"] {
					t.Fatalf("args: got %s want %s", args, c.args)
				}
			}
		})
	}
}

func TestEmptyOrArtifactOnlyFinalAnswerIsAnError(t *testing.T) {
	for _, content := range []string{"", "   ", "<|channel>"} {
		f := &fakeLLM{replies: []llm.Message{{Role: "assistant", Content: content}}}
		a := &Agent{LLM: f, Tools: setup(t)}
		got, err := a.Run(context.Background(), "x")
		if err == nil || !strings.Contains(err.Error(), "vazia") || got != "" {
			t.Errorf("conteúdo %q deveria virar erro de resposta vazia; got %q err %v", content, got, err)
		}
	}
	// resposta com marcador no meio é aproveitada, só limpa
	f := &fakeLLM{replies: []llm.Message{{Role: "assistant", Content: "<|channel>Resposta."}}}
	if got, err := (&Agent{LLM: f, Tools: setup(t)}).Run(context.Background(), "x"); err != nil || got != "Resposta." {
		t.Errorf("got %q, %v", got, err)
	}
}
