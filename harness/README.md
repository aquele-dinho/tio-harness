# tioh-agent — avaliador e agente em Go

Camada de **orquestração (Layer 2)** do Tio Harness, em Go. O CLI Python `tioh` instala o *grounding*
(Layer 1: `AGENTS.md`, specs, skills) nos projetos; o `tioh-agent` conversa com um modelo local e
executa as ferramentas que o modelo pede, sob regras do harness (Layer 3 = o modelo/agente em si).

Dois comandos principais: **`eval`** mede se o harness instalado melhora um modelo; **`ask`** é o loop de function
calling (somente leitura). Uma dependência externa (Cobra).

## Requisitos
- Go 1.26+.
- Um servidor compatível com OpenAI rodando: **LM Studio** (padrão, `http://localhost:1234/v1`) ou
  **Ollama** (`--base-url http://localhost:11434/v1`).
- Um modelo que faça *tool calls* de verdade. Testado: `google/gemma-4-12b`. Modelos que "fingem" a chamada
  (ex.: Qwen 2.5 Coder em alguns setups) caem no fallback de bloco JSON ou inventam resultado: confira o rastro.

## Uso
```bash
cd harness
go build -o bin/tioh-agent ./cmd/tioh-agent

./bin/tioh-agent models                               # servidor no ar? quais modelos?
./bin/tioh-agent tools --root ..                      # ferramentas que o modelo recebe
./bin/tioh-agent ask "Quais packs o README lista?" --root ..
echo "resuma o AGENTS.md" | ./bin/tioh-agent ask - --root ..
```
O rastro das ferramentas vai para o **stderr** (`--quiet` desliga); só a resposta vai para o stdout.
Resposta vazia (ou só com marcadores vazados como `<|channel>`, comum em modelos locais com ferramentas) sai com
**erro**, nunca como sucesso silencioso.

## `eval`: o harness funciona?
```bash
./bin/tioh-agent eval --project ~/Projects/meu-projeto
./bin/tioh-agent eval --project . --only gap:,recall: --conditions harness --json resultado.json
./bin/tioh-agent eval --project . --min-pass 0.8 --min-lift 0.3      # gate: sai 1 se não entregar
```
Gera sondas do `.ai-framework/project.json` instalado e pergunta ao modelo em 3 condições:
`neutral` (sem harness), `generic` (sem harness, com a regra "não invente") e `harness` (o `AGENTS.md` instalado
como prompt de sistema). Correção por regex, sem LLM-juiz.

| Sonda | Pergunta | Acerta quando |
|---|---|---|
| `recall:*` | branch, comando de teste, chave Jira/space, ticket âncora, ID da fonte | contém o valor do briefing |
| `states:*` | estados de cada máquina de estados | cita todos os estados |
| `commit-format` | mensagem de commit para o ticket âncora | `tipo(TICKET): …`, sem `Co-Authored-By` se a regra valer |
| `gap:*` | campo marcado `A DEFINIR` | admite que é indefinido (usar o nome do responsável como valor é erro) |
| `control:*` | portão humano, número sem fonte | controles: se passam **sem** harness, não provam nada |

Leitura do resultado: compare `projeto (acertos)` do `harness` com a **melhor** linha de base (o `ganho`). Respostas
`EMPTY` contam como erro. Limites: amostra pequena, temperatura 0, um modelo; mede lembrança e obediência a regras em
prosa, não qualidade de código ou comportamento em tarefas longas. `--max-tokens` (padrão 1000; truncadas são repetidas com 4x) precisa deixar
espaço para modelos que "pensam" antes de responder.

| Flag / env | Padrão | O que faz |
|---|---|---|
| `--base-url` / `TIOH_LLM_URL` | `http://localhost:1234/v1` | servidor da API |
| `--model` / `TIOH_MODEL` | `google/gemma-4-12b` | modelo (conferido no servidor antes de chamar) |
| `--api-key` / `TIOH_API_KEY` | vazio | só se o servidor exigir |
| `--root` | `.` | pasta que o modelo pode ler (nada fora dela) |
| `--max-steps` | 8 | voltas modelo↔ferramentas antes de desistir |
| `--max-bytes` | 16384 | bytes por leitura (contexto local é curto: ~24k tokens) |
| `--max-tokens` | 2048 | saída por chamada |
| `--system-file` | — | substitui o prompt de sistema padrão |

## Como funciona
```
pergunta → [system + user] → modelo ──tool_calls?──► sim: harness executa (Go) → resultado volta ao modelo ─┐
                               ▲                         não: resposta final                                │
                               └────────────────────────────────────────────────────────────────────────────┘
```
1. **Function calling nativo** (`tool_calls` da API). 2. **Fallback do blueprint**: se o modelo escreve no texto
um bloco `{"tool": "read_file", "path": "main.go"}` (com ou sem cerca ```json), o harness também executa.
Erros de ferramenta voltam ao modelo **como texto** (`ERROR: …`) para ele se corrigir; só o limite de passos aborta.

## Segurança (o modelo é entrada não confiável)
- Ferramentas **somente leitura** (`read_file`, `list_dir`), confinadas a `--root`.
- Caminhos são resolvidos com `EvalSymlinks`: `..`, caminho absoluto e **symlink que aponta para fora** são negados.
- Fora da raiz, "não existe" e "proibido" dão a **mesma** mensagem: o modelo não consegue sondar o que existe
  fora do sandbox (um teste cobre isso).
- Binários são recusados e leituras são truncadas em `--max-bytes`.
- Não há execução de comandos nesta fase; escrita e `exec` virão com *gates* humanos (ver `AGENTS.md` do repo).

## Desenvolvimento
```bash
cd harness
gofmt -l . ; go vet ./... && go test ./... -race
```
Os testes usam um modelo falso (loop) e `httptest` (cliente HTTP): rodam sem LM Studio. Para um teste real,
rode o `ask` acima e **confira a resposta contra o arquivo**: não confie no relato do modelo.

## Próximos passos
Rodar o `eval` em CI exige um modelo acessível ao runner (hoje só local). Integrar Jira/GitHub continua no `tioh`
(Python, pack `atlassian`).
