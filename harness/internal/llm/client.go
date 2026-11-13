// Package llm é um cliente mínimo da API de chat compatível com OpenAI, que é o que LM Studio e Ollama
// (porta :11434/v1) expõem. Só o necessário para o loop de function calling.
package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// Message é uma mensagem da conversa. Role: system | user | assistant | tool.
type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
}

// ToolCall é um pedido do modelo para executar uma ferramenta.
type ToolCall struct {
	ID       string       `json:"id"`
	Type     string       `json:"type"`
	Function FunctionCall `json:"function"`
}

// FunctionCall: Arguments chega como string JSON (formato OpenAI).
type FunctionCall struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// ToolDef descreve uma ferramenta oferecida ao modelo.
type ToolDef struct {
	Type     string      `json:"type"`
	Function FunctionDef `json:"function"`
}

type FunctionDef struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

// Client fala com um servidor compatível com OpenAI. APIKey costuma ser ignorada por servidores locais.
type Client struct {
	BaseURL string // ex.: http://localhost:1234/v1
	APIKey  string
	Model   string
	HTTP    *http.Client
}

// New devolve um cliente com timeout generoso: modelo local frio pode levar minutos para carregar.
func New(baseURL, apiKey, model string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		APIKey:  apiKey,
		Model:   model,
		HTTP:    &http.Client{Timeout: 10 * time.Minute},
	}
}

type chatRequest struct {
	Model       string    `json:"model"`
	Messages    []Message `json:"messages"`
	Tools       []ToolDef `json:"tools,omitempty"`
	ToolChoice  string    `json:"tool_choice,omitempty"`
	Temperature float64   `json:"temperature"`
	MaxTokens   int       `json:"max_tokens,omitempty"`
}

type chatResponse struct {
	Choices []struct {
		Message      Message `json:"message"`
		FinishReason string  `json:"finish_reason"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
	} `json:"usage"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

// Usage é o consumo de tokens informado pelo servidor.
type Usage struct {
	PromptTokens     int
	CompletionTokens int
	// Truncated: o modelo parou por limite de tokens (finish_reason=length). Em modelos com raciocínio, isso costuma
	// significar que gastou tudo pensando e o conteúdo veio vazio: não é "o modelo não soube responder".
	Truncated bool
}

// specialToken casa marcadores de template que alguns modelos locais deixam vazar no texto (ex.: <|channel>).
var specialToken = regexp.MustCompile(`<\|[^<>]*>|<[^<>|]*\|>`)

// CleanContent remove marcadores especiais vazados e espaços nas pontas. Vazio = o modelo não respondeu.
func CleanContent(s string) string {
	return strings.TrimSpace(specialToken.ReplaceAllString(s, ""))
}

// Chat envia a conversa e devolve a mensagem do assistente (que pode trazer ToolCalls).
func (c *Client) Chat(ctx context.Context, msgs []Message, tools []ToolDef, maxTokens int) (Message, error) {
	req := chatRequest{Model: c.Model, Messages: msgs, Tools: tools, Temperature: 0, MaxTokens: maxTokens}
	if len(tools) > 0 {
		req.ToolChoice = "auto"
	}
	var resp chatResponse
	if err := c.do(ctx, http.MethodPost, "/chat/completions", req, &resp); err != nil {
		return Message{}, err
	}
	if resp.Error != nil {
		return Message{}, fmt.Errorf("servidor LLM: %s", resp.Error.Message)
	}
	if len(resp.Choices) == 0 {
		return Message{}, fmt.Errorf("servidor LLM respondeu sem escolhas")
	}
	m := resp.Choices[0].Message
	if m.Role == "" {
		m.Role = "assistant"
	}
	return m, nil
}

// Complete faz uma pergunta simples (sem ferramentas) e devolve o texto já limpo e o consumo de tokens.
// É o que o avaliador usa: sem ferramentas, a resposta depende só do prompt de sistema.
func (c *Client) Complete(ctx context.Context, system, user string, maxTokens int) (string, Usage, error) {
	req := chatRequest{Model: c.Model, Temperature: 0, MaxTokens: maxTokens, Messages: []Message{
		{Role: "system", Content: system}, {Role: "user", Content: user}}}
	var resp chatResponse
	if err := c.do(ctx, http.MethodPost, "/chat/completions", req, &resp); err != nil {
		return "", Usage{}, err
	}
	if resp.Error != nil {
		return "", Usage{}, fmt.Errorf("servidor LLM: %s", resp.Error.Message)
	}
	if len(resp.Choices) == 0 {
		return "", Usage{}, fmt.Errorf("servidor LLM respondeu sem escolhas")
	}
	ch := resp.Choices[0]
	return CleanContent(ch.Message.Content), Usage{resp.Usage.PromptTokens, resp.Usage.CompletionTokens, ch.FinishReason == "length"}, nil
}

// Models lista os IDs de modelos que o servidor expõe.
func (c *Client) Models(ctx context.Context) ([]string, error) {
	var resp struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := c.do(ctx, http.MethodGet, "/models", nil, &resp); err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(resp.Data))
	for _, m := range resp.Data {
		ids = append(ids, m.ID)
	}
	return ids, nil
}

func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, rd)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	res, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("não consegui falar com o servidor LLM em %s (LM Studio/Ollama está rodando?): %w", c.BaseURL, err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 32<<20))
	if err != nil {
		return err
	}
	if res.StatusCode/100 != 2 {
		snippet := strings.TrimSpace(string(raw))
		if len(snippet) > 400 {
			snippet = snippet[:400] + "…"
		}
		return fmt.Errorf("servidor LLM respondeu %d em %s: %s", res.StatusCode, path, snippet)
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("resposta do servidor LLM inválida em %s: %w", path, err)
	}
	return nil
}
