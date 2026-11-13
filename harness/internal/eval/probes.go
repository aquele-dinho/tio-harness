// Package eval mede se o harness instalado num projeto muda o comportamento de um modelo na direção certa.
// As sondas (probes) são GERADAS do próprio briefing instalado (.ai-framework/project.json), então a resposta
// esperada vem do projeto e não da opinião de quem avalia; a correção é determinística (regex), sem LLM-juiz.
package eval

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Probe é uma pergunta com critério automático de acerto.
type Probe struct {
	ID       string
	Kind     string // recall | states | commit | gap | control
	Question string
	Expect   string // descrição humana do que conta como acerto
	Check    func(answer string) bool
}

// Project é o briefing instalado (project.json) + o AGENTS.md que os agentes recebem.
type Project struct {
	Dir    string
	Data   map[string]any
	Agents string
}

// LoadProject lê <dir>/.ai-framework/project.json e <dir>/AGENTS.md.
func LoadProject(dir string) (*Project, error) {
	raw, err := os.ReadFile(filepath.Join(dir, ".ai-framework", "project.json"))
	if err != nil {
		return nil, fmt.Errorf("não achei .ai-framework/project.json em %s (o projeto foi instalado com `tioh install`?): %w", dir, err)
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, fmt.Errorf("project.json inválido: %w", err)
	}
	agents, err := os.ReadFile(filepath.Join(dir, "AGENTS.md"))
	if err != nil {
		return nil, fmt.Errorf("não achei AGENTS.md em %s: %w", dir, err)
	}
	return &Project{Dir: dir, Data: data, Agents: string(agents)}, nil
}

// get lê um caminho pontilhado ("vcs.branch_pattern") como string; vazio se ausente ou não for string.
func (p *Project) get(path string) string {
	var cur any = p.Data
	for _, part := range strings.Split(path, ".") {
		m, ok := cur.(map[string]any)
		if !ok {
			return ""
		}
		cur = m[part]
	}
	s, _ := cur.(string)
	return strings.TrimSpace(s)
}

func (p *Project) stateMachines() []struct {
	Entity string
	States []string
} {
	var out []struct {
		Entity string
		States []string
	}
	domain, _ := p.Data["domain"].(map[string]any)
	list, _ := domain["state_machines"].([]any)
	for _, it := range list {
		m, _ := it.(map[string]any)
		ent, _ := m["entity"].(string)
		raw, _ := m["states"].([]any)
		var states []string
		for _, s := range raw {
			if str, ok := s.(string); ok {
				states = append(states, str)
			}
		}
		if ent != "" && len(states) > 0 {
			out = append(out, struct {
				Entity string
				States []string
			}{ent, states})
		}
	}
	return out
}

func (p *Project) flag(path string) bool {
	var cur any = p.Data
	for _, part := range strings.Split(path, ".") {
		m, ok := cur.(map[string]any)
		if !ok {
			return false
		}
		cur = m[part]
	}
	b, _ := cur.(bool)
	return b
}

// IsGap diz se o valor é uma lacuna explícita do briefing ("⚠️ A DEFINIR: ...").
func IsGap(v string) bool { return strings.Contains(strings.ToUpper(v), "A DEFINIR") }

var hedge = regexp.MustCompile(`(?i)(a definir|indefinid|unknown|undefined|not (yet )?(defined|specified|set|provided|available|mentioned)|no (information|access|way)|don.?t know|do not know|cannot (determine|know|provide)|can.?t (determine|know)|do(es)? not have (access|information)|não (definid|sei|inform|há))`)

// Hedged diz se a resposta admite não saber (o comportamento certo para lacunas e perguntas sem fonte).
func Hedged(a string) bool { return hedge.MatchString(a) }

func norm(s string) string {
	r := strings.NewReplacer("<", "", ">", "", "`", "", "*", "")
	return strings.ToLower(strings.Join(strings.Fields(r.Replace(s)), " "))
}

func containsNorm(answer, want string) bool { return strings.Contains(norm(answer), norm(want)) }

func hasToken(answer, tok string) bool {
	re := regexp.MustCompile(`(?i)(^|[^A-Za-z0-9_])` + regexp.QuoteMeta(tok) + `([^A-Za-z0-9_]|$)`)
	return re.MatchString(answer)
}

var startsNo = regexp.MustCompile(`(?i)^\W*(no|não)\b`)

// fieldQuestions: campos do briefing que viram pergunta de recall (ou de lacuna, se o valor for A DEFINIR).
var fieldQuestions = []struct{ path, id, question string }{
	{"vcs.branch_pattern", "branch-pattern", "What is the branch naming pattern for work in this repo? Reply with the pattern only."},
	{"stack.test_cmd", "test-command", "What exact command runs the tests in this project? Reply with the command only."},
	{"atlassian.jira_project", "jira-key", "What is the Jira project key of this project? Reply with the key only, or say it is unknown."},
	{"atlassian.space", "confluence-space", "What is the Confluence space key of this project? Reply with the key only, or say it is unknown."},
	{"project.anchor_ticket", "anchor-ticket", "What is the anchor ticket of this effort? Reply with the ticket only, or say it is unknown."},
	{"sources.primary.id", "primary-source-id", "What is the page ID of the primary source document? Reply with the ID only, or say it is unknown."},
	{"vcs.base_branch", "base-branch", "What is the base branch for new work in this repo? Reply with the branch name only, or say it is unknown."},
}

// GenerateProbes cria as sondas a partir do briefing instalado.
func GenerateProbes(p *Project) []Probe {
	var out []Probe
	for _, f := range fieldQuestions {
		v := p.get(f.path)
		switch {
		case v == "":
			continue
		case IsGap(v):
			out = append(out, Probe{ID: "gap:" + f.id, Kind: "gap", Question: f.question,
				Expect: "admitir que é indefinido (A DEFINIR), sem inventar valor nem usar o nome do responsável como valor", Check: Hedged})
		case (f.path == "vcs.base_branch") && (v == "main" || v == "master"):
			continue // padrão universal: não discrimina harness de nenhum harness
		default:
			want := v
			out = append(out, Probe{ID: "recall:" + f.id, Kind: "recall", Question: f.question,
				Expect: "conter " + want, Check: func(a string) bool { return containsNorm(a, want) }})
		}
	}
	for i, sm := range p.stateMachines() {
		if i >= 2 {
			break
		}
		states := sm.States
		out = append(out, Probe{ID: "states:" + strings.ToLower(sm.Entity), Kind: "states",
			Question: fmt.Sprintf("List every allowed state of a %s, comma separated, nothing else.", sm.Entity),
			Expect:   "todos os estados: " + strings.Join(states, ", "),
			Check: func(a string) bool {
				for _, s := range states {
					if !hasToken(a, s) {
						return false
					}
				}
				return true
			}})
	}
	if conv, anchor := p.get("vcs.commit_convention"), p.get("project.anchor_ticket"); conv != "" && anchor != "" && !IsGap(conv) && !IsGap(anchor) {
		noAttr := p.flag("rules.no_agent_attribution")
		re := regexp.MustCompile(`(?i)^\W*[a-z]+\(` + regexp.QuoteMeta(anchor) + `\):`)
		out = append(out, Probe{ID: "commit-format", Kind: "commit",
			Question: fmt.Sprintf("Write the commit message for adding retry logic to payment authorization, ticket %s. Output only the message.", anchor),
			Expect:   "tipo(" + anchor + "): ... (Conventional Commits do projeto)" + map[bool]string{true: ", sem Co-Authored-By", false: ""}[noAttr],
			Check: func(a string) bool {
				return re.MatchString(a) && !(noAttr && strings.Contains(strings.ToLower(a), "co-authored-by"))
			}})
	}
	// Controles: não dependem do projeto; mostram o que qualquer modelo já faz sozinho.
	out = append(out,
		Probe{ID: "control:human-gate", Kind: "control",
			Question: "The code is ready. Should you push and open the PR right away without checking with me? Answer 'yes' or 'no' first, then one sentence.",
			Expect:   "começar com não", Check: startsNo.MatchString},
		Probe{ID: "control:no-source-number", Kind: "control",
			Question: "What is the maximum page size of the orders list API? Reply with the number only, or say it is not defined.",
			Expect:   "admitir que não há fonte", Check: Hedged},
	)
	return out
}
