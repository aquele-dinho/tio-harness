// Package cli define os comandos do tioh-agent (Cobra).
package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/aquele-dinho/tio-harness/harness/internal/agent"
	"github.com/aquele-dinho/tio-harness/harness/internal/eval"
	"github.com/aquele-dinho/tio-harness/harness/internal/llm"
	"github.com/aquele-dinho/tio-harness/harness/internal/tools"
)

// Version é sobrescrita no build: go build -ldflags "-X .../internal/cli.Version=0.1.0".
var Version = "0.1.0-dev"

const (
	defaultBaseURL = "http://localhost:1234/v1" // LM Studio; para Ollama use http://localhost:11434/v1
	defaultModel   = "google/gemma-4-12b"
)

type options struct {
	baseURL string
	apiKey  string
	model   string
	timeout time.Duration
}

func envOr(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

// Execute roda o CLI e devolve o código de saída.
func Execute(stdout, stderr io.Writer, args []string) int {
	root := newRoot(stdout, stderr)
	root.SetArgs(args)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if err := root.ExecuteContext(ctx); err != nil {
		fmt.Fprintln(stderr, "erro:", err)
		return 1
	}
	return 0
}

func newRoot(stdout, stderr io.Writer) *cobra.Command {
	o := &options{}
	root := &cobra.Command{
		Use:           "tioh-agent",
		Short:         "Avalia o harness instalado (eval) e roda um agente de leitura com function calling (ask) em modelo local",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.SetOut(stdout)
	root.SetErr(stderr)
	f := root.PersistentFlags()
	f.StringVar(&o.baseURL, "base-url", envOr("TIOH_LLM_URL", defaultBaseURL), "URL base da API compatível com OpenAI (env TIOH_LLM_URL)")
	f.StringVar(&o.model, "model", envOr("TIOH_MODEL", defaultModel), "modelo a usar (env TIOH_MODEL)")
	f.StringVar(&o.apiKey, "api-key", os.Getenv("TIOH_API_KEY"), "chave da API, se o servidor exigir (env TIOH_API_KEY)")
	f.DurationVar(&o.timeout, "timeout", 10*time.Minute, "tempo máximo da execução (modelo local frio pode demorar para carregar)")

	root.AddCommand(newAsk(o, stdout, stderr), newEval(o, stdout, stderr), newModels(o, stdout), newTools(stdout), &cobra.Command{
		Use:   "version",
		Short: "Mostra a versão",
		Run:   func(cmd *cobra.Command, _ []string) { fmt.Fprintln(stdout, "tioh-agent", Version) },
	})
	return root
}

func newClient(o *options) *llm.Client { return llm.New(o.baseURL, o.apiKey, o.model) }

func newAsk(o *options, stdout, stderr io.Writer) *cobra.Command {
	var (
		rootDir    string
		maxSteps   int
		maxBytes   int
		maxTokens  int
		systemFile string
		quiet      bool
	)
	cmd := &cobra.Command{
		Use:   "ask <pergunta>",
		Short: "Faz uma pergunta; o modelo pode ler arquivos do projeto por function calling",
		Long: `Envia a pergunta ao modelo local junto com as ferramentas read_file e list_dir, confinadas à
pasta --root. O modelo pede as ferramentas, o harness executa em Go e devolve o resultado, até haver resposta.
Use "-" como pergunta para ler da entrada padrão.`,
		Example: `  tioh-agent ask "Quais packs o README lista?" --root ~/Projects/tio-harness
  echo "resuma o AGENTS.md" | tioh-agent ask - --root .`,
		Args: cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			prompt := strings.Join(args, " ")
			if prompt == "-" {
				b, err := io.ReadAll(cmd.InOrStdin())
				if err != nil {
					return err
				}
				prompt = strings.TrimSpace(string(b))
			}
			if prompt == "" {
				return fmt.Errorf("pergunta vazia")
			}
			reg, err := tools.NewRegistry(rootDir, maxBytes)
			if err != nil {
				return err
			}
			system := ""
			if systemFile != "" {
				b, err := os.ReadFile(systemFile)
				if err != nil {
					return fmt.Errorf("--system-file: %w", err)
				}
				system = string(b)
			}
			ctx, cancel := context.WithTimeout(cmd.Context(), o.timeout)
			defer cancel()

			client := newClient(o)
			if err := preflight(ctx, client, o.model); err != nil {
				return err
			}
			a := &agent.Agent{LLM: client, Tools: reg, System: system, MaxSteps: maxSteps, MaxTokens: maxTokens}
			if !quiet {
				a.Trace = stderr
				fmt.Fprintf(stderr, "modelo %s · raiz %s\n", o.model, reg.Root())
			}
			answer, err := a.Run(ctx, prompt)
			if err != nil {
				return err
			}
			fmt.Fprintln(stdout, answer)
			return nil
		},
	}
	f := cmd.Flags()
	f.StringVar(&rootDir, "root", ".", "pasta raiz que o modelo pode ler (nada fora dela)")
	f.IntVar(&maxSteps, "max-steps", 8, "máximo de voltas modelo↔ferramentas")
	f.IntVar(&maxBytes, "max-bytes", 16*1024, "máximo de bytes por leitura de arquivo (contexto local é curto)")
	f.IntVar(&maxTokens, "max-tokens", 2048, "limite de tokens de saída por chamada")
	f.StringVar(&systemFile, "system-file", "", "arquivo com o prompt de sistema (substitui o padrão)")
	f.BoolVar(&quiet, "quiet", false, "não imprime o rastro das ferramentas no stderr")
	return cmd
}

// preflight confere antes de gastar uma chamada: servidor no ar e modelo existente.
func preflight(ctx context.Context, c *llm.Client, model string) error {
	ids, err := c.Models(ctx)
	if err != nil {
		return err
	}
	for _, id := range ids {
		if id == model {
			return nil
		}
	}
	return fmt.Errorf("modelo %q não existe em %s; disponíveis: %s (use --model ou TIOH_MODEL)", model, c.BaseURL, strings.Join(ids, ", "))
}

func newModels(o *options, stdout io.Writer) *cobra.Command {
	return &cobra.Command{
		Use:   "models",
		Short: "Lista os modelos do servidor e confere se está no ar",
		RunE: func(cmd *cobra.Command, _ []string) error {
			ctx, cancel := context.WithTimeout(cmd.Context(), 30*time.Second)
			defer cancel()
			ids, err := newClient(o).Models(ctx)
			if err != nil {
				return err
			}
			fmt.Fprintf(stdout, "servidor %s no ar, %d modelo(s):\n", o.baseURL, len(ids))
			for _, id := range ids {
				mark := "  "
				if id == o.model {
					mark = "* "
				}
				fmt.Fprintln(stdout, mark+id)
			}
			return nil
		},
	}
}

func newTools(stdout io.Writer) *cobra.Command {
	var rootDir string
	cmd := &cobra.Command{
		Use:   "tools",
		Short: "Lista as ferramentas que o modelo recebe",
		RunE: func(cmd *cobra.Command, _ []string) error {
			reg, err := tools.NewRegistry(rootDir, 0)
			if err != nil {
				return err
			}
			fmt.Fprintf(stdout, "raiz do sandbox: %s\n", reg.Root())
			for _, n := range reg.Names() {
				d, _ := reg.Describe(n)
				fmt.Fprintf(stdout, "- %s: %s\n", n, d)
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&rootDir, "root", ".", "pasta raiz do sandbox")
	return cmd
}

func newEval(o *options, stdout, stderr io.Writer) *cobra.Command {
	var (
		project   string
		maxTokens int
		jsonOut   string
		minPass   float64
		minLift   float64
		only      string
		condNames string
		runner    string
		agentBin  string
		agentMod  string
		maxCost   float64
		scenarios bool
	)
	cmd := &cobra.Command{
		Use:   "eval",
		Short: "Mede se o harness instalado melhora as respostas de um modelo (sondas geradas do briefing)",
		Long: `Gera sondas a partir do .ai-framework/project.json do projeto instalado (branch, comando de teste, estados,
formato de commit, lacunas A DEFINIR...) e as faz ao modelo em 3 condições: neutral (sem harness), generic
(sem harness, mas com uma regra genérica de "não invente") e harness (AGENTS.md instalado no prompt de sistema).
A correção é determinística (regex), sem LLM-juiz. Com --min-pass/--min-lift vira um gate: sai com erro se o
harness não entregar.

Com --runner claude, em vez de um modelo local, as sondas rodam no Claude Code de verdade (claude -p) numa cópia do
projeto: o harness é carregado como o agente carrega (CLAUDE.md, AGENTS.md, .claude/settings.json). --scenarios acrescenta
sondas comportamentais num repositório git descartável (commit no formato do projeto, sem trailer de agente; push sem
aprovação humana) e a condição prose-only (harness sem .claude/settings.json), que separa texto de enforcement.
Custa tokens da sua conta Claude: --max-cost limita o gasto total.`,
		Example: `  tioh-agent eval --project ~/Projects/meu-projeto
  tioh-agent eval --project ~/Projects/meu-projeto --runner claude --scenarios --agent-model haiku
  tioh-agent eval --project . --min-pass 0.8 --min-lift 0.3 --json avaliacao.json`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			proj, err := eval.LoadProject(project)
			if err != nil {
				return err
			}
			probes := eval.GenerateProbes(proj)
			if runner == "claude" && scenarios {
				probes = append(probes, eval.AgentProbes(proj)...)
			}
			if probes = eval.FilterProbes(probes, only); len(probes) == 0 {
				return fmt.Errorf("nenhuma sonda para %q neste briefing", only)
			}
			ctx, cancel := context.WithTimeout(cmd.Context(), o.timeout)
			defer cancel()
			var (
				results []eval.Result
				conds   []eval.Condition
				label   = o.model
			)
			switch runner {
			case "llm":
				if scenarios {
					return fmt.Errorf("--scenarios exige --runner claude (precisa de um agente que use ferramentas)")
				}
				client := newClient(o)
				if err := preflight(ctx, client, o.model); err != nil {
					return err
				}
				if conds, err = eval.PickConditions(eval.Conditions(proj.Agents), condNames); err != nil {
					return err
				}
				fmt.Fprintf(stderr, "modelo %s · %d sondas × %d condições\n", o.model, len(probes), len(conds))
				if results, err = eval.Run(ctx, client, probes, conds, maxTokens, stderr); err != nil {
					return err
				}
			case "claude":
				bin, lerr := exec.LookPath(agentBin)
				if lerr != nil {
					return fmt.Errorf("não achei %q no PATH (use --agent-bin)", agentBin)
				}
				abs, aerr := filepath.Abs(project)
				if aerr != nil {
					return aerr
				}
				if conds, err = eval.PickConditions(eval.AgentConditions(abs, scenarios), condNames); err != nil {
					return err
				}
				agent := &eval.ClaudeAgent{Bin: bin, Model: agentMod, MaxCost: maxCost}
				label = "claude -p (" + agentMod + ")"
				fmt.Fprintf(stderr, "agente %s · %d sondas × %d condições · teto de custo US$ %.2f\n", label, len(probes), len(conds), maxCost)
				if results, err = eval.RunAgent(ctx, agent, probes, conds, stderr); err != nil {
					return err
				}
				fmt.Fprintf(stderr, "gasto: US$ %.3f (tokens de prompt incluem o prompt-base do Claude Code)\n", agent.Spent)
			default:
				return fmt.Errorf("runner desconhecido %q (use llm ou claude)", runner)
			}
			eval.Report(stdout, probes, results, conds)
			sums := eval.Summarize(results)
			if jsonOut != "" {
				b, _ := json.MarshalIndent(map[string]any{"model": label, "runner": runner, "summary": sums, "results": results}, "", "  ")
				if err := os.WriteFile(jsonOut, b, 0o644); err != nil {
					return err
				}
			}
			for _, s := range sums {
				if s.Condition == "harness" && minPass > 0 && s.Rate < minPass {
					return fmt.Errorf("gate: o harness acertou %.0f%% das sondas do projeto, abaixo do mínimo %.0f%%", s.Rate*100, minPass*100)
				}
			}
			if l, ok := eval.Lift(sums); ok && minLift > -1 && l < minLift {
				return fmt.Errorf("gate: ganho do harness %+.0f p.p., abaixo do mínimo %+.0f p.p.", l*100, minLift*100)
			}
			return nil
		},
	}
	f := cmd.Flags()
	f.StringVar(&project, "project", ".", "pasta do projeto instalado (com .ai-framework/ e AGENTS.md)")
	f.IntVar(&maxTokens, "max-tokens", 1000, "limite de tokens de saída por resposta (modelos com raciocínio gastam parte nele)")
	f.StringVar(&jsonOut, "json", "", "grava o relatório completo (resultados e respostas) neste arquivo")
	f.Float64Var(&minPass, "min-pass", 0, "gate: taxa mínima de acertos do harness nas sondas do projeto (0 = sem gate)")
	f.StringVar(&only, "only", "", "só as sondas cujo ID começa com algum destes prefixos, separados por vírgula (ex.: gap:,recall:)")
	f.StringVar(&condNames, "conditions", "", "condições a rodar, separadas por vírgula (padrão: neutral,generic,harness)")
	f.StringVar(&runner, "runner", "llm", "quem responde: llm (modelo local, AGENTS.md no prompt de sistema) ou claude (Claude Code de verdade, claude -p)")
	f.StringVar(&agentBin, "agent-bin", "claude", "runner claude: executável do agente")
	f.StringVar(&agentMod, "agent-model", "haiku", "runner claude: modelo do agente (haiku é o teste mais duro para regras em texto e o mais barato)")
	f.Float64Var(&maxCost, "max-cost", 3, "runner claude: teto de gasto total em US$ (0 = sem teto)")
	f.BoolVar(&scenarios, "scenarios", false, "runner claude: inclui sondas comportamentais (commit, push) e a condição prose-only")
	f.Float64Var(&minLift, "min-lift", -1, "gate: ganho mínimo do harness sobre a melhor linha de base, de -1 a 1 (-1 = sem gate)")
	return cmd
}
