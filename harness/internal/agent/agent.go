// Package agent implementa o loop da Fase 1 do blueprint: o modelo pede uma ferramenta (function calling),
// o harness executa em Go e devolve o resultado, até o modelo dar a resposta final.
package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/aquele-dinho/tio-harness/harness/internal/llm"
	"github.com/aquele-dinho/tio-harness/harness/internal/tools"
)

// DefaultSystem é o prompt de sistema mínimo do harness: força o uso das ferramentas em vez de chutar.
const DefaultSystem = `You are a careful engineering assistant working inside a project directory.
You can only learn about the project through the tools you are given (read_file, list_dir).
Rules:
- Never invent file contents, names or facts. If you have not read it with a tool, you do not know it.
- If a tool reports an error (missing file, access denied), say so plainly instead of guessing.
- Read only what you need, then answer concisely, in the same language as the user's question.`

// Chatter é o que o loop precisa do cliente LLM (permite testar com um falso).
type Chatter interface {
	Chat(ctx context.Context, msgs []llm.Message, defs []llm.ToolDef, maxTokens int) (llm.Message, error)
}

// Agent amarra modelo, ferramentas e limites.
type Agent struct {
	LLM       Chatter
	Tools     *tools.Registry
	System    string    // vazio = DefaultSystem
	MaxSteps  int       // voltas modelo↔ferramentas antes de desistir (0 = 8)
	MaxTokens int       // limite de saída por chamada (0 = 2048)
	Trace     io.Writer // se não nil, recebe o rastro das ferramentas
}

// Run responde ao prompt, usando ferramentas quantas vezes o modelo pedir (até MaxSteps).
func (a *Agent) Run(ctx context.Context, prompt string) (string, error) {
	system := a.System
	if strings.TrimSpace(system) == "" {
		system = DefaultSystem
	}
	steps := a.MaxSteps
	if steps <= 0 {
		steps = 8
	}
	maxTokens := a.MaxTokens
	if maxTokens <= 0 {
		maxTokens = 2048
	}
	msgs := []llm.Message{{Role: "system", Content: system}, {Role: "user", Content: prompt}}
	defs := a.Tools.Defs()

	for step := 1; step <= steps; step++ {
		reply, err := a.LLM.Chat(ctx, msgs, defs, maxTokens)
		if err != nil {
			return "", err
		}

		// 1) function calling nativo (tool_calls)
		if len(reply.ToolCalls) > 0 {
			msgs = append(msgs, reply)
			for _, tc := range reply.ToolCalls {
				out := a.exec(tc.Function.Name, json.RawMessage(tc.Function.Arguments))
				msgs = append(msgs, llm.Message{Role: "tool", ToolCallID: tc.ID, Content: out})
			}
			continue
		}

		// 2) fallback do blueprint: bloco JSON {"tool": "read_file", "path": "..."} no texto
		if name, args, ok := ParseToolBlock(reply.Content); ok {
			msgs = append(msgs, reply)
			out := a.exec(name, args)
			msgs = append(msgs, llm.Message{Role: "user", Content: fmt.Sprintf(
				"Tool result for %s:\n%s\n\nContinue. Answer the original question once you have enough information.", name, out)})
			continue
		}

		// 3) resposta final (vazia ou só com marcadores vazados = o modelo não respondeu: erro, não sucesso silencioso)
		final := llm.CleanContent(reply.Content)
		if final == "" {
			return "", fmt.Errorf("o modelo devolveu resposta vazia (comum com alguns modelos locais quando há ferramentas); tente de novo, outro --model ou menos contexto")
		}
		return final, nil
	}
	return "", fmt.Errorf("o modelo não chegou a uma resposta final em %d passos (aumente --max-steps ou simplifique a pergunta)", steps)
}

// exec roda a ferramenta e devolve sempre texto: erros vão para o modelo, que pode se corrigir.
func (a *Agent) exec(name string, args json.RawMessage) string {
	a.trace("→ %s %s\n", name, compact(args))
	out, err := a.Tools.Call(name, args)
	if err != nil {
		a.trace("  ✗ %v\n", err)
		return "ERROR: " + err.Error()
	}
	a.trace("  ✓ %d bytes\n", len(out))
	return out
}

func (a *Agent) trace(format string, v ...any) {
	if a.Trace != nil {
		fmt.Fprintf(a.Trace, format, v...)
	}
}

func compact(raw json.RawMessage) string {
	s := strings.TrimSpace(string(raw))
	if len(s) > 160 {
		s = s[:160] + "…"
	}
	return s
}

// ParseToolBlock procura no texto um objeto JSON {"tool": "<nome>", ...args} (o formato descrito no blueprint),
// com ou sem cerca ```json. Os demais campos viram os argumentos; "arguments"/"args" como objeto também valem.
func ParseToolBlock(content string) (name string, args json.RawMessage, ok bool) {
	for i := 0; i < len(content); i++ {
		if content[i] != '{' {
			continue
		}
		var obj map[string]json.RawMessage
		dec := json.NewDecoder(strings.NewReader(content[i:]))
		if err := dec.Decode(&obj); err != nil {
			continue
		}
		rawName, has := obj["tool"]
		if !has {
			continue
		}
		if err := json.Unmarshal(rawName, &name); err != nil || name == "" {
			continue
		}
		delete(obj, "tool")
		for _, k := range []string{"arguments", "args"} {
			if inner, has := obj[k]; has {
				var nested map[string]json.RawMessage
				if json.Unmarshal(inner, &nested) == nil {
					obj = nested
				}
				break
			}
		}
		b, err := json.Marshal(obj)
		if err != nil {
			continue
		}
		return name, b, true
	}
	return "", nil, false
}
