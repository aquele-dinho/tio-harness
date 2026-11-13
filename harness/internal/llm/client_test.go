package llm

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestChatSendsToolsAndParsesToolCalls(t *testing.T) {
	var gotAuth string
	var gotReq map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" || r.Method != http.MethodPost {
			t.Errorf("rota inesperada: %s %s", r.Method, r.URL.Path)
		}
		gotAuth = r.Header.Get("Authorization")
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &gotReq)
		io.WriteString(w, `{"choices":[{"message":{"role":"assistant","content":"","tool_calls":[
			{"id":"call_1","type":"function","function":{"name":"read_file","arguments":"{\"path\":\"main.go\"}"}}]}}]}`)
	}))
	defer srv.Close()

	c := New(srv.URL+"/v1/", "chave", "modelo-x") // barra final deve ser tolerada
	defs := []ToolDef{{Type: "function", Function: FunctionDef{Name: "read_file", Description: "d", Parameters: json.RawMessage(`{"type":"object"}`)}}}
	msg, err := c.Chat(context.Background(), []Message{{Role: "user", Content: "oi"}}, defs, 123)
	if err != nil {
		t.Fatal(err)
	}
	if gotAuth != "Bearer chave" {
		t.Errorf("Authorization: %q", gotAuth)
	}
	if gotReq["model"] != "modelo-x" || gotReq["tool_choice"] != "auto" || gotReq["temperature"] != float64(0) || gotReq["max_tokens"] != float64(123) {
		t.Errorf("requisição inesperada: %v", gotReq)
	}
	if tools, _ := gotReq["tools"].([]any); len(tools) != 1 {
		t.Errorf("tools não enviadas: %v", gotReq["tools"])
	}
	if len(msg.ToolCalls) != 1 || msg.ToolCalls[0].ID != "call_1" || msg.ToolCalls[0].Function.Name != "read_file" ||
		msg.ToolCalls[0].Function.Arguments != `{"path":"main.go"}` {
		t.Errorf("tool_calls mal interpretadas: %+v", msg)
	}
}

func TestChatWithoutToolsOmitsToolChoice(t *testing.T) {
	var gotReq map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &gotReq)
		io.WriteString(w, `{"choices":[{"message":{"content":"olá"}}]}`)
	}))
	defer srv.Close()
	msg, err := New(srv.URL, "", "m").Chat(context.Background(), []Message{{Role: "user", Content: "oi"}}, nil, 0)
	if err != nil || msg.Content != "olá" || msg.Role != "assistant" {
		t.Fatalf("got %+v, %v", msg, err)
	}
	if _, has := gotReq["tool_choice"]; has {
		t.Errorf("tool_choice não deveria ser enviado sem ferramentas: %v", gotReq)
	}
}

func TestModels(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"data":[{"id":"a/b"},{"id":"c"}]}`)
	}))
	defer srv.Close()
	ids, err := New(srv.URL, "", "").Models(context.Background())
	if err != nil || len(ids) != 2 || ids[0] != "a/b" || ids[1] != "c" {
		t.Fatalf("got %v, %v", ids, err)
	}
}

func TestErrors(t *testing.T) {
	// HTTP 500 com corpo
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		io.WriteString(w, "modelo não carregado")
	}))
	_, err := New(srv.URL, "", "m").Chat(context.Background(), nil, nil, 0)
	srv.Close()
	if err == nil || !strings.Contains(err.Error(), "500") || !strings.Contains(err.Error(), "modelo não carregado") {
		t.Errorf("erro HTTP deveria trazer status e corpo: %v", err)
	}

	// erro no corpo JSON com status 200
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"error":{"message":"sem memória"}}`)
	}))
	_, err = New(srv.URL, "", "m").Chat(context.Background(), nil, nil, 0)
	srv.Close()
	if err == nil || !strings.Contains(err.Error(), "sem memória") {
		t.Errorf("erro do corpo deveria aparecer: %v", err)
	}

	// resposta que não é JSON
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "<html>") }))
	_, err = New(srv.URL, "", "m").Models(context.Background())
	srv.Close()
	if err == nil || !strings.Contains(err.Error(), "inválida") {
		t.Errorf("JSON inválido deveria falhar: %v", err)
	}

	// servidor fora do ar: mensagem orienta o usuário
	_, err = New("http://127.0.0.1:1", "", "m").Models(context.Background())
	if err == nil || !strings.Contains(err.Error(), "LM Studio/Ollama") {
		t.Errorf("servidor fora do ar deveria orientar: %v", err)
	}
}

func TestCleanContent(t *testing.T) {
	cases := map[string]string{
		"<|channel>":           "",
		"  <|channel>  ":       "",
		"<|channel>resposta":   "resposta",
		"texto <|im_end|> fim": "texto  fim",
		"<channel|>ok":         "ok",
		"a < b e c > d":        "a < b e c > d", // comparação comum não é marcador
		"vector<int> x":        "vector<int> x",
		"  normal  ":           "normal",
	}
	for in, want := range cases {
		if got := CleanContent(in); got != want {
			t.Errorf("CleanContent(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestCompleteReportsUsageAndCleans(t *testing.T) {
	var gotReq map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &gotReq)
		io.WriteString(w, `{"choices":[{"message":{"content":"<|channel> feature/x "}}],"usage":{"prompt_tokens":3398,"completion_tokens":7}}`)
	}))
	defer srv.Close()
	text, use, err := New(srv.URL, "", "m").Complete(context.Background(), "sys", "pergunta", 50)
	if err != nil || text != "feature/x" || use.PromptTokens != 3398 || use.CompletionTokens != 7 {
		t.Fatalf("got %q %+v %v", text, use, err)
	}
	if _, has := gotReq["tools"]; has {
		t.Error("Complete não deve enviar ferramentas")
	}
	msgs, _ := gotReq["messages"].([]any)
	if len(msgs) != 2 || gotReq["temperature"] != float64(0) {
		t.Errorf("requisição inesperada: %v", gotReq)
	}
}

func TestCompleteFlagsTruncation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"choices":[{"message":{"content":""},"finish_reason":"length"}],"usage":{"prompt_tokens":10,"completion_tokens":999}}`)
	}))
	defer srv.Close()
	text, use, err := New(srv.URL, "", "m").Complete(context.Background(), "s", "q", 1000)
	if err != nil || text != "" || !use.Truncated {
		t.Fatalf("got %q %+v %v", text, use, err)
	}
}
