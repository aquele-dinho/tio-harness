package eval

import (
	"context"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/aquele-dinho/tio-harness/harness/internal/llm"
)

// retryFactor multiplica o orçamento de tokens na nova tentativa após truncamento.
const retryFactor = 4

// Completer é o que o avaliador precisa do modelo (permite testar com um falso).
type Completer interface {
	Complete(ctx context.Context, system, user string, maxTokens int) (string, llm.Usage, error)
}

// Condition é um jeito de montar o prompt de sistema.
type Condition struct{ Name, System string }

const (
	neutralSystem = "You are a helpful engineering assistant."
	genericSystem = "You are a careful engineering assistant. Never invent file contents, names or facts. If you do not know, say so plainly."
)

// Conditions devolve as condições da comparação:
//   - neutral: sem harness, prompt neutro (linha de base);
//   - generic: sem harness, mas com uma boa regra genérica de "não invente" (o que um prompt de uma linha já dá);
//   - harness: prompt neutro + o AGENTS.md instalado, carregado como os agentes carregam.
func Conditions(agentsMD string) []Condition {
	return []Condition{
		{"neutral", neutralSystem},
		{"generic", genericSystem},
		{"harness", neutralSystem + "\n\n# Project instructions (AGENTS.md, auto-loaded)\n" + agentsMD},
	}
}

// PickConditions filtra as condições por nome (lista separada por vírgula; vazio = todas).
func PickConditions(all []Condition, names string) ([]Condition, error) {
	if strings.TrimSpace(names) == "" {
		return all, nil
	}
	var out []Condition
	for _, n := range strings.Split(names, ",") {
		n = strings.TrimSpace(n)
		found := false
		for _, c := range all {
			if c.Name == n {
				out, found = append(out, c), true
			}
		}
		if !found {
			return nil, fmt.Errorf("condição desconhecida %q (use neutral, generic ou harness)", n)
		}
	}
	return out, nil
}

// FilterProbes mantém só as sondas cujo ID começa com um dos prefixos (vazio = todas).
func FilterProbes(probes []Probe, prefixes string) []Probe {
	if strings.TrimSpace(prefixes) == "" {
		return probes
	}
	var out []Probe
	for _, p := range probes {
		for _, pre := range strings.Split(prefixes, ",") {
			if pre = strings.TrimSpace(pre); pre != "" && strings.HasPrefix(p.ID, pre) {
				out = append(out, p)
				break
			}
		}
	}
	return out
}

// Result é uma sonda numa condição.
type Result struct {
	Probe        string `json:"probe"`
	Kind         string `json:"kind"`
	Condition    string `json:"condition"`
	Pass         bool   `json:"pass"`
	Empty        bool   `json:"empty"`
	Truncated    bool   `json:"truncated"`
	Retried      bool   `json:"retried"`
	Answer       string `json:"answer"`
	PromptTokens int    `json:"prompt_tokens"`
}

// Run executa cada sonda em cada condição. Falha de servidor aborta (resultado parcial enganaria).
func Run(ctx context.Context, c Completer, probes []Probe, conds []Condition, maxTokens int, progress io.Writer) ([]Result, error) {
	var out []Result
	for _, p := range probes {
		for _, cd := range conds {
			ans, use, err := c.Complete(ctx, cd.System, p.Question, maxTokens)
			if err != nil {
				return nil, fmt.Errorf("sonda %s [%s]: %w", p.ID, cd.Name, err)
			}
			retried := false
			if ans == "" && use.Truncated { // modelo de raciocínio gastou o orçamento pensando: tenta de novo com mais espaço
				retried = true
				if ans, use, err = c.Complete(ctx, cd.System, p.Question, maxTokens*retryFactor); err != nil {
					return nil, fmt.Errorf("sonda %s [%s] (nova tentativa): %w", p.ID, cd.Name, err)
				}
			}
			r := Result{Probe: p.ID, Kind: p.Kind, Condition: cd.Name, Answer: ans, PromptTokens: use.PromptTokens,
				Empty: ans == "", Truncated: ans == "" && use.Truncated, Retried: retried}
			r.Pass = !r.Empty && p.Check(ans)
			out = append(out, r)
			if progress != nil {
				fmt.Fprintf(progress, ".")
			}
		}
	}
	if progress != nil {
		fmt.Fprintln(progress)
	}
	return out, nil
}

// Summary agrega os resultados por condição, separando sondas de projeto (discriminam) dos controles.
type Summary struct {
	Condition    string  `json:"condition"`
	ProjectPass  int     `json:"project_pass"`
	ProjectTotal int     `json:"project_total"`
	ControlPass  int     `json:"control_pass"`
	ControlTotal int     `json:"control_total"`
	Empty        int     `json:"empty"`
	Truncated    int     `json:"truncated"`
	AvgPrompt    int     `json:"avg_prompt_tokens"`
	Rate         float64 `json:"project_pass_rate"`
}

// Summarize calcula o resumo por condição, na ordem em que as condições aparecem.
func Summarize(rs []Result) []Summary {
	idx := map[string]*Summary{}
	var order []string
	tok := map[string][]int{}
	for _, r := range rs {
		s, ok := idx[r.Condition]
		if !ok {
			s = &Summary{Condition: r.Condition}
			idx[r.Condition] = s
			order = append(order, r.Condition)
		}
		if r.Kind == "control" {
			s.ControlTotal++
			if r.Pass {
				s.ControlPass++
			}
		} else {
			s.ProjectTotal++
			if r.Pass {
				s.ProjectPass++
			}
		}
		if r.Empty {
			s.Empty++
		}
		if r.Truncated {
			s.Truncated++
		}
		tok[r.Condition] = append(tok[r.Condition], r.PromptTokens)
	}
	out := make([]Summary, 0, len(order))
	for _, n := range order {
		s := idx[n]
		if s.ProjectTotal > 0 {
			s.Rate = float64(s.ProjectPass) / float64(s.ProjectTotal)
		}
		sum := 0
		for _, t := range tok[n] {
			sum += t
		}
		if len(tok[n]) > 0 {
			s.AvgPrompt = sum / len(tok[n])
		}
		out = append(out, *s)
	}
	return out
}

// Lift é a taxa do harness menos a melhor taxa sem harness (neutral/generic): o ganho que só o harness explica.
func Lift(sums []Summary) (lift float64, ok bool) {
	var harness *Summary
	best := -1.0
	for i := range sums {
		if sums[i].Condition == "harness" {
			harness = &sums[i]
		} else if sums[i].Rate > best {
			best = sums[i].Rate
		}
	}
	if harness == nil || best < 0 {
		return 0, false
	}
	return harness.Rate - best, true
}

// Report imprime a tabela sonda × condição e os totais.
func Report(w io.Writer, probes []Probe, rs []Result, conds []Condition) {
	by := map[string]Result{}
	for _, r := range rs {
		by[r.Probe+"|"+r.Condition] = r
	}
	fmt.Fprintf(w, "%-28s", "sonda")
	for _, c := range conds {
		fmt.Fprintf(w, "%-10s", c.Name)
	}
	fmt.Fprintln(w)
	for _, p := range probes {
		fmt.Fprintf(w, "%-28s", p.ID)
		for _, c := range conds {
			r := by[p.ID+"|"+c.Name]
			mark := "fail"
			switch {
			case r.Truncated:
				mark = "TRUNC"
			case r.Empty:
				mark = "EMPTY"
			case r.Pass:
				mark = "PASS"
			}
			fmt.Fprintf(w, "%-10s", mark)
		}
		fmt.Fprintln(w)
	}
	sums := Summarize(rs)
	fmt.Fprintln(w, strings.Repeat("-", 28+10*len(conds)))
	fmt.Fprintf(w, "%-28s", "projeto (acertos)")
	for _, s := range sums {
		fmt.Fprintf(w, "%-10s", fmt.Sprintf("%d/%d", s.ProjectPass, s.ProjectTotal))
	}
	fmt.Fprintf(w, "\n%-28s", "controles (acertos)")
	for _, s := range sums {
		fmt.Fprintf(w, "%-10s", fmt.Sprintf("%d/%d", s.ControlPass, s.ControlTotal))
	}
	fmt.Fprintf(w, "\n%-28s", "tokens de prompt (média)")
	for _, s := range sums {
		fmt.Fprintf(w, "%-10d", s.AvgPrompt)
	}
	fmt.Fprintln(w)
	for _, s := range sums {
		if s.Truncated > 0 {
			fmt.Fprintf(w, "AVISO: %d resposta(s) de %q truncadas — o resultado subestima o modelo; aumente --max-tokens\n", s.Truncated, s.Condition)
		}
	}
	if l, ok := Lift(sums); ok {
		fmt.Fprintf(w, "ganho do harness sobre a melhor linha de base: %+.0f pontos percentuais\n", l*100)
	}
	// respostas que erraram sob o harness: é onde mora a melhoria
	var bad []Result
	for _, r := range rs {
		if r.Condition == "harness" && !r.Pass && r.Kind != "control" {
			bad = append(bad, r)
		}
	}
	sort.Slice(bad, func(i, j int) bool { return bad[i].Probe < bad[j].Probe })
	for _, r := range bad {
		a := strings.Join(strings.Fields(r.Answer), " ")
		if len(a) > 160 {
			a = a[:160] + "…"
		}
		switch {
		case r.Truncated:
			a = "(truncada por limite de tokens mesmo após nova tentativa: aumente --max-tokens; não é erro do modelo)"
		case r.Empty:
			a = "(resposta vazia)"
		}
		fmt.Fprintf(w, "ERRO sob o harness · %s → %q\n", r.Probe, a)
	}
}
