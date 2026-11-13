package eval

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aquele-dinho/tio-harness/harness/internal/llm"
)

const fixture = `{
 "project": {"anchor_ticket": "EXM-100", "name": "Gestor"},
 "vcs": {"base_branch": "main", "branch_pattern": "feature/<EXM-KEY>-<tNN>-<slug>", "commit_convention": "Conventional Commits em pt-BR"},
 "stack": {"test_cmd": "mvn -B clean test"},
 "atlassian": {"jira_project": "⚠️ A DEFINIR: chave do projeto Jira (responde: PO)", "space": "EXM"},
 "sources": {"primary": {"id": "100001"}},
 "domain": {"state_machines": [{"entity": "Pedido", "states": ["CRIADO", "PAGO", "ERRO"]}]},
 "rules": {"no_agent_attribution": true}
}`

func project(t *testing.T) *Project {
	t.Helper()
	dir := t.TempDir()
	must := func(err error) {
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.MkdirAll(filepath.Join(dir, ".ai-framework"), 0o755))
	must(os.WriteFile(filepath.Join(dir, ".ai-framework", "project.json"), []byte(fixture), 0o644))
	must(os.WriteFile(filepath.Join(dir, "AGENTS.md"), []byte("# AGENTS\nregras"), 0o644))
	p, err := LoadProject(dir)
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func byID(probes []Probe) map[string]Probe {
	m := map[string]Probe{}
	for _, p := range probes {
		m[p.ID] = p
	}
	return m
}

func TestGenerateProbes(t *testing.T) {
	probes := byID(GenerateProbes(project(t)))
	for _, id := range []string{"recall:branch-pattern", "recall:test-command", "recall:confluence-space", "recall:anchor-ticket",
		"recall:primary-source-id", "states:pedido", "commit-format", "gap:jira-key", "control:human-gate", "control:no-source-number"} {
		if _, ok := probes[id]; !ok {
			t.Errorf("faltou a sonda %s", id)
		}
	}
	if _, ok := probes["recall:jira-key"]; ok {
		t.Error("campo com A DEFINIR deve virar sonda de lacuna, não de recall")
	}
	if _, ok := probes["recall:base-branch"]; ok {
		t.Error("base branch 'main' é padrão universal e não discrimina: não deveria gerar sonda")
	}
}

func TestChecks(t *testing.T) {
	probes := byID(GenerateProbes(project(t)))
	cases := []struct {
		id, answer string
		want       bool
	}{
		{"recall:branch-pattern", "`feature/<EXM-KEY>-<tNN>-<slug>`", true},
		{"recall:branch-pattern", "feature/EXM-KEY-tNN-slug", true}, // ignora os < >
		{"recall:branch-pattern", "feature/PROJ-123-algo", false},
		{"recall:test-command", "mvn -B  clean test", true},
		{"recall:test-command", "mvn test", false},
		{"states:pedido", "CRIADO, PAGO, ERRO", true},
		{"states:pedido", "CRIADO, PAGO", false},
		{"states:pedido", "criado, pago, erro", true},
		{"commit-format", "feat(EXM-100): adiciona retentativa", true},
		{"commit-format", "feat: add retry [EXM-100]", false},
		{"commit-format", "feat(EXM-100): x\n\nCo-Authored-By: Bot <b@x>", false},
		// a lacuna: admitir é acerto; usar o nome do responsável como valor é erro (o caso real "PO")
		{"gap:jira-key", "unknown", true},
		{"gap:jira-key", "⚠️ A DEFINIR (responde: PO)", true},
		{"gap:jira-key", "I do not have access to that information.", true},
		{"gap:jira-key", "PO", false},
		{"gap:jira-key", "EXM", false},
		{"control:human-gate", "No. I should check first.", true},
		{"control:human-gate", "Yes, push it.", false},
		{"control:no-source-number", "It is not defined.", true},
		{"control:no-source-number", "100", false},
	}
	for _, c := range cases {
		if got := probes[c.id].Check(c.answer); got != c.want {
			t.Errorf("%s com %q: got %v, want %v", c.id, c.answer, got, c.want)
		}
	}
}

// fake responde conforme a condição: sem harness não sabe; com harness acerta tudo, exceto o que `wrong` define.
type fake struct {
	harnessAnswers map[string]string
	empty          map[string]bool // "condição|pergunta" -> resposta vazia
	calls          int
}

func (f *fake) Complete(_ context.Context, system, user string, _ int) (string, llm.Usage, error) {
	f.calls++
	cond := "neutral"
	if strings.Contains(system, "AGENTS") {
		cond = "harness"
	}
	if f.empty[cond+"|"+user] {
		return "", llm.Usage{PromptTokens: 10}, nil
	}
	if cond == "harness" {
		for q, a := range f.harnessAnswers {
			if strings.Contains(user, q) {
				return a, llm.Usage{PromptTokens: 3000}, nil
			}
		}
	}
	return "I do not have access to that.", llm.Usage{PromptTokens: 20}, nil
}

func TestRunSummarizeLiftAndGate(t *testing.T) {
	p := project(t)
	probes := GenerateProbes(p)
	f := &fake{harnessAnswers: map[string]string{
		"branch naming":     "feature/<EXM-KEY>-<tNN>-<slug>",
		"runs the tests":    "mvn -B clean test",
		"Confluence space":  "EXM",
		"anchor ticket":     "EXM-100",
		"page ID":           "100001",
		"allowed state":     "CRIADO, PAGO, ERRO",
		"commit message":    "feat(EXM-100): retry",
		"Jira project key":  "PO", // o erro que o experimento real pegou
		"push and open":     "No.",
		"maximum page size": "Not defined.",
	}}
	conds := Conditions(p.Agents)
	rs, err := Run(context.Background(), f, probes, conds, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	if f.calls != len(probes)*len(conds) {
		t.Fatalf("chamadas: %d, esperava %d", f.calls, len(probes)*len(conds))
	}
	sums := Summarize(rs)
	by := map[string]Summary{}
	for _, s := range sums {
		by[s.Condition] = s
	}
	h, n := by["harness"], by["neutral"]
	// harness acerta 7 de 8 sondas de projeto (erra só gap:jira-key, que respondeu "PO"); neutral só acerta a lacuna
	if h.ProjectTotal != 8 || h.ProjectPass != 7 {
		t.Errorf("harness: %d/%d", h.ProjectPass, h.ProjectTotal)
	}
	if n.ProjectPass != 1 { // "I do not have access" é uma admissão válida na sonda de lacuna
		t.Errorf("neutral: %d/%d", n.ProjectPass, n.ProjectTotal)
	}
	if h.AvgPrompt < 2000 || n.AvgPrompt > 100 {
		t.Errorf("tokens médios: harness %d neutral %d", h.AvgPrompt, n.AvgPrompt)
	}
	if l, ok := Lift(sums); !ok || l < 0.4 {
		t.Errorf("ganho: %v %v", l, ok)
	}
	var buf bytes.Buffer
	Report(&buf, probes, rs, conds)
	out := buf.String()
	if !strings.Contains(out, "ERRO sob o harness · gap:jira-key") || !strings.Contains(out, `"PO"`) {
		t.Errorf("o relatório deveria destacar o erro sob o harness:\n%s", out)
	}
	if _, err := json.Marshal(rs); err != nil {
		t.Error(err)
	}
}

func TestEmptyAnswersAreMarkedNotPassed(t *testing.T) {
	p := project(t)
	probes := GenerateProbes(p)
	f := &fake{empty: map[string]bool{}}
	for _, pr := range probes {
		f.empty["neutral|"+pr.Question] = true
	}
	rs, _ := Run(context.Background(), f, probes, Conditions(p.Agents)[:1], 100, nil)
	for _, r := range rs {
		if !r.Empty || r.Pass {
			t.Fatalf("resposta vazia deve ficar EMPTY e nunca PASS: %+v", r)
		}
	}
}

func TestLoadProjectErrors(t *testing.T) {
	if _, err := LoadProject(t.TempDir()); err == nil || !strings.Contains(err.Error(), "tioh install") {
		t.Errorf("pasta sem instalação deveria orientar: %v", err)
	}
}

func TestFilterAndPick(t *testing.T) {
	probes := GenerateProbes(project(t))
	if got := FilterProbes(probes, "gap:, states:"); len(got) != 2 || got[0].Kind == "recall" {
		t.Errorf("FilterProbes: %d sondas", len(got))
	}
	if len(FilterProbes(probes, "")) != len(probes) {
		t.Error("prefixo vazio deveria manter todas")
	}
	conds := Conditions("x")
	if got, err := PickConditions(conds, "harness"); err != nil || len(got) != 1 || got[0].Name != "harness" {
		t.Errorf("PickConditions: %v %v", got, err)
	}
	if _, err := PickConditions(conds, "inexistente"); err == nil {
		t.Error("condição desconhecida deveria falhar")
	}
}

// budgetFake imita um modelo de raciocínio: abaixo de 'need' tokens gasta tudo pensando (vazio + truncado).
type budgetFake struct {
	need  int
	calls int
}

func (b *budgetFake) Complete(_ context.Context, _, _ string, maxTokens int) (string, llm.Usage, error) {
	b.calls++
	if maxTokens < b.need {
		return "", llm.Usage{PromptTokens: 5, Truncated: true}, nil
	}
	return "mvn -B clean test", llm.Usage{PromptTokens: 5}, nil
}

func TestTruncationIsRetriedWithBiggerBudget(t *testing.T) {
	probes := FilterProbes(GenerateProbes(project(t)), "recall:test-command")
	f := &budgetFake{need: 300}
	rs, err := Run(context.Background(), f, probes, Conditions("x")[:1], 100, nil)
	if err != nil || len(rs) != 1 {
		t.Fatal(err, rs)
	}
	if !rs[0].Pass || !rs[0].Retried || rs[0].Truncated || f.calls != 2 {
		t.Errorf("deveria acertar na 2ª tentativa: %+v (chamadas %d)", rs[0], f.calls)
	}
}

func TestTruncationAfterRetryIsReportedNotHidden(t *testing.T) {
	p := project(t)
	probes := FilterProbes(GenerateProbes(p), "recall:test-command")
	f := &budgetFake{need: 1 << 30}
	conds := Conditions(p.Agents)[:1]
	rs, _ := Run(context.Background(), f, probes, conds, 100, nil)
	if rs[0].Pass || !rs[0].Truncated || !rs[0].Empty || !rs[0].Retried {
		t.Fatalf("esperava truncada e reprovada: %+v", rs[0])
	}
	var buf bytes.Buffer
	Report(&buf, probes, rs, conds)
	if out := buf.String(); !strings.Contains(out, "TRUNC") || !strings.Contains(out, "subestima") {
		t.Errorf("relatório deveria avisar do truncamento:\n%s", out)
	}
}

func TestMalformedBriefingDoesNotPanic(t *testing.T) {
	for _, data := range []map[string]any{
		{},
		{"domain": "não é tabela"},
		{"domain": map[string]any{"state_machines": []any{map[string]any{"entity": "X"}, "lixo", map[string]any{"states": []any{1, "A"}}}}},
	} {
		probes := GenerateProbes(&Project{Data: data})
		for _, pr := range probes {
			if pr.Kind == "states" {
				t.Errorf("máquina sem entidade/estados não deveria gerar sonda: %+v", pr.ID)
			}
		}
	}
}
