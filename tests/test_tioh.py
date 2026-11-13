"""Testes do CLI tioh (somente stdlib): python3 -m unittest discover -s tests -v

Cobrem o que a avaliação do harness apontou: validação rasa, lacunas lidas como valor, instalação
determinística/idempotente, detecção de drift e a matriz pack × agente.
"""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIOH = ROOT / "bin" / "tioh.py"
EXAMPLE = ROOT / "examples" / "gestor-pedidos.briefing.toml"

_spec = importlib.util.spec_from_file_location("tioh", TIOH)
tioh = importlib.util.module_from_spec(_spec)
sys.modules["tioh"] = tioh
_spec.loader.exec_module(tioh)


def example():
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


def minimal(**over):
    """Briefing mínimo válido (só o pack core), com sobrescritas por seção."""
    d = {"project": {"name": "Mini", "slug": "mini", "description": "d", "output_language": "pt-BR"},
         "sources": {"primary": {"title": "DRN"}},
         "sdd": {"unit": "módulo", "spec_dir": "docs/sdd/modulos"},
         "agents": {"targets": ["claude"]}, "packs": {"enabled": ["core"]}}
    for k, v in over.items():
        d[k] = {**d.get(k, {}), **v} if isinstance(v, dict) else v
    return d


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def briefing(self, data, name="b.json"):
        p = self.tmp / name
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def target(self, name="proj"):
        t = self.tmp / name
        t.mkdir(exist_ok=True)
        return t

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, str(TIOH), *map(str, args)], capture_output=True, text=True, encoding="utf-8")
        return r.returncode, r.stdout + r.stderr


class GapText(unittest.TestCase):
    def test_owner_is_not_left_looking_like_the_value(self):
        out = tioh.gap_text("⚠️ A DEFINIR: chave do projeto Jira (responde: PO)")
        self.assertIn("valor indefinido", out)
        self.assertIn("decide: PO", out)
        self.assertNotIn("(responde: PO)", out)

    def test_non_gaps_pass_through(self):
        for v in ("EXM", "", "feature/<X>", 42, None, ["a"]):
            self.assertEqual(tioh.gap_text(v), v)

    def test_prose_that_mentions_the_marker_is_untouched(self):
        for v in ("Marque ⚠️ A DEFINIR quando faltar fonte", "Validar regra X (A DEFINIR com o PO)"):
            self.assertEqual(tioh.gap_text(v), v)

    def test_gap_variants_are_recognized(self):
        for v in ("A DEFINIR: x", "⚠️ A DEFINIR: x", "⚠ A DEFINIR: x", "> ⚠️ A DEFINIR: x", "  A DEFINIR"):
            self.assertIn("valor indefinido", tioh.gap_text(v), v)

    def test_gap_without_owner_or_detail(self):
        self.assertEqual(tioh.gap_text("A DEFINIR"), "A DEFINIR (valor indefinido, não presumir)")

    def test_installed_agents_md_never_shows_raw_owner_marker(self):
        d = example()
        d["atlassian"]["jira_project"] = "⚠️ A DEFINIR: chave do projeto Jira (responde: PO)"
        out = tioh.plan_files(tioh.resolve_packs(d["packs"]["enabled"]), tioh.build_context(d))
        text = out["AGENTS.md"]["content"].decode("utf-8")
        self.assertIn("decide: PO", text)
        self.assertNotIn("(responde: PO)", text)
        self.assertIn("A DEFINIR` não é valor", text)           # a regra explícita está no AGENTS.md


class Validation(Workspace):
    def errors(self, data):
        errs, warns, _ = tioh.diagnose(data)
        return errs, warns

    def test_example_and_minimal_are_clean(self):
        for d in (example(), minimal()):
            errs, warns = self.errors(d)
            self.assertEqual((errs, warns), ([], []))

    def test_unknown_agent_is_an_error_with_suggestion(self):
        errs, _ = self.errors(minimal(agents={"targets": ["claud"]}))
        self.assertTrue(any("claud" in e and "claude" in e for e in errs), errs)
        errs, _ = self.errors(minimal(agents={"targets": ["vim"]}))
        self.assertTrue(any("vim" in e for e in errs), errs)

    def test_wrong_types(self):
        errs, _ = self.errors(minimal(agents={"targets": "claude"}))
        self.assertTrue(any("agents.targets" in e and "lista" in e for e in errs), errs)
        errs, _ = self.errors(minimal(project={"name": 7}))
        self.assertTrue(any("project.name" in e and "texto" in e for e in errs), errs)
        errs, _ = self.errors(minimal(vcs={"host": "bitbucket"}))
        self.assertTrue(any("vcs.host" in e for e in errs), errs)

    def test_typos_warn_but_do_not_block(self):
        errs, warns = self.errors(minimal(project={"nme": "x"}, sdd={"spek_dir": "y"}))
        self.assertEqual(errs, [])
        self.assertTrue(any("project.nme" in w and "name" in w for w in warns), warns)
        self.assertTrue(any("sdd.spek_dir" in w and "spec_dir" in w for w in warns), warns)

    def test_unknown_pack_suggests(self):
        errs, _ = self.errors(minimal(packs={"enabled": ["core", "atlasian"]}))
        self.assertTrue(any("atlasian" in e and "atlassian" in e for e in errs), errs)

    def test_missing_pack_prerequisites_are_all_reported_at_once(self):
        errs, _ = self.errors(minimal(packs={"enabled": ["core", "atlassian"]}))
        joined = "\n".join(errs)
        for key in ("jira_project", "site", "space"):
            self.assertIn(key, joined)
        self.assertIn("usado por", joined)
        errs, _ = self.errors(minimal(packs={"enabled": ["core", "java-spring-hexagonal"]}))
        self.assertTrue(any("language" in e for e in errs) and any("framework" in e for e in errs), errs)

    def test_missing_required_field(self):
        d = minimal()
        del d["project"]["slug"]
        errs, _ = self.errors(d)
        self.assertTrue(any("project.slug" in e for e in errs), errs)

    def test_cli_exit_codes_and_no_traceback_on_broken_toml(self):
        ok = self.briefing(minimal())
        self.assertEqual(self.run_cli("validate", ok)[0], 0)
        bad = self.briefing(minimal(agents={"targets": ["vim"]}), "bad.json")
        code, out = self.run_cli("validate", bad)
        self.assertEqual(code, 1)
        self.assertIn("ERRO", out)
        broken = self.tmp / "broken.toml"
        broken.write_text("[project\nname=", encoding="utf-8")
        code, out = self.run_cli("validate", broken)
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", out)
        self.assertIn("ilegível", out)

    def test_install_refuses_invalid_briefing_and_writes_nothing(self):
        t = self.target()
        code, out = self.run_cli("install", self.briefing(minimal(agents={"targets": ["vim"]})), t)
        self.assertNotEqual(code, 0)
        self.assertEqual(list(t.iterdir()), [])


class InstallBehavior(Workspace):
    def install(self, data, target=None, *extra):
        t = target or self.target()
        code, out = self.run_cli("install", self.briefing(data), t, *extra)
        self.assertEqual(code, 0, out)
        return t

    @staticmethod
    def tree(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
                if p.is_file() and ".ai-framework" not in p.parts}

    def test_deterministic(self):
        a, b = self.install(example(), self.target("a")), self.install(example(), self.target("b"))
        self.assertEqual(self.tree(a), self.tree(b))
        self.assertGreater(len(self.tree(a)), 50)

    def test_idempotent_and_check_clean(self):
        t = self.install(example())
        before = self.tree(t)
        code, out = self.run_cli("install", self.briefing(example()), t)
        self.assertEqual(code, 0)
        self.assertEqual(before, self.tree(t))
        self.assertEqual(self.run_cli("check", t)[0], 0)

    def test_no_unrendered_placeholders_outside_scripts(self):
        t = self.install(example())
        bad = [rel for rel, b in self.tree(t).items()
               if b"@@" in b and not rel.startswith("scripts/") and "/scripts/" not in rel]
        self.assertEqual(bad, [])

    def test_drift_detected_and_local_edit_preserved(self):
        t = self.install(example())
        f = t / "AGENTS.md"
        f.write_text(f.read_text(encoding="utf-8") + "\nedição local\n", encoding="utf-8")
        code, out = self.run_cli("check", t)
        self.assertEqual(code, 1)
        self.assertIn("ALTERADO", out)
        # reinstalar não sobrescreve: grava .tioh-new
        d = example()
        d["project"]["description"] = "descrição nova"
        self.install(d, t)
        self.assertIn("edição local", f.read_text(encoding="utf-8"))
        self.assertTrue((t / "AGENTS.md.tioh-new").exists())

    def test_crlf_checkout_is_not_drift(self):
        t = self.install(example())
        for p in t.rglob("*.md"):
            if ".ai-framework" in p.parts:
                continue
            raw = p.read_bytes()
            if b"\0" not in raw:
                p.write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        code, out = self.run_cli("check", t)
        self.assertEqual(code, 0, out)

    def test_dry_run_writes_nothing(self):
        t = self.target()
        code, out = self.run_cli("install", self.briefing(example()), t, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(list(t.iterdir()), [])

    def test_refuses_framework_as_target(self):
        code, _ = self.run_cli("install", self.briefing(example()), ROOT / "packs")
        self.assertNotEqual(code, 0)


class Matrix(Workspace):
    """Cada pack sozinho (com o core) × cada agente: tem que instalar e ficar em dia."""

    POINTERS = {"claude": "CLAUDE.md", "gemini": "GEMINI.md", "warp": "WARP.md",
                "copilot": ".github/copilot-instructions.md", "codex": "AGENTS.md"}

    def test_every_pack_installs_with_a_complete_briefing(self):
        packs = sorted(p.parent.name for p in (ROOT / "packs").glob("*/pack.toml"))
        self.assertGreaterEqual(len(packs), 9)
        for pack in packs:
            d = example()
            d["packs"]["enabled"] = sorted({"core", pack})
            t = self.target(f"p-{pack}")
            code, out = self.run_cli("install", self.briefing(d, f"{pack}.json"), t)
            self.assertEqual(code, 0, f"{pack}: {out}")
            self.assertEqual(self.run_cli("check", t)[0], 0, pack)

    def test_every_agent_gets_its_pointer_file(self):
        for agent, rel in self.POINTERS.items():
            d = example()
            d["agents"]["targets"] = [agent]
            t = self.target(f"a-{agent}")
            code, out = self.run_cli("install", self.briefing(d, f"{agent}.json"), t)
            self.assertEqual(code, 0, f"{agent}: {out}")
            self.assertTrue((t / rel).is_file(), f"{agent}: faltou {rel}")


GATE = ROOT / "packs" / "quality-gates" / "files" / "scripts" / "sdd" / "gate.py"
_gspec = importlib.util.spec_from_file_location("gate", GATE)
gate = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(gate)


class GatePure(unittest.TestCase):
    def test_commit_subjects(self):
        ok = ["feat(EXM-100): adiciona retentativa", "fix: corrige timeout", "chore!: remove legado",
              "docs(readme): ajusta\n\ncorpo livre"]
        bad = ["adiciona retentativa", "Feat(EXM-1): x", "feat(EXM-1) x", "feat(EXM-1):", "", "WIP"]
        for m in ok:
            self.assertEqual(gate.commit_problems(m), [], m)
        for m in bad:
            self.assertTrue(gate.commit_problems(m), m)

    def test_agent_attribution_blocked_only_when_rule_is_on(self):
        for tail in ("Co-Authored-By: Claude <noreply@anthropic.com>", "Co-authored-by: GitHub Copilot <c@x>",
                     "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)"):
            msg = "feat(EXM-1): x\n\n" + tail
            self.assertTrue(gate.commit_problems(msg, True), tail)
            self.assertEqual(gate.commit_problems(msg, False), [], tail)
        # texto comum e reverts do git não são violação
        self.assertEqual(gate.commit_problems("docs: regenera clientes generated with protoc", True), [])
        self.assertEqual(gate.commit_problems('Revert "feat(EXM-1): x"', True), [])
        self.assertTrue(gate.commit_problems("feat(EXM-1): x\n\nGenerated by Copilot", True))
        # co-autor humano é legítimo
        self.assertEqual(gate.commit_problems("feat(EXM-1): x\n\nCo-authored-by: Maria <maria@x.com>", True), [])

    def test_branch_regex(self):
        rx = gate.branch_regex("feature/<EXM-KEY>-<tNN>-<slug-curto>")
        self.assertTrue(rx.match("feature/EXM-12-t03-retentativa"))
        self.assertFalse(rx.match("feature/EXM-12"))
        self.assertFalse(rx.match("hotfix/EXM-12-t03-x"))
        self.assertFalse(rx.match("feature/EXM-12-t03-a/b"))
        self.assertFalse(rx.match("xfeature/EXM-12-t03-x"))


class GateEndToEnd(Workspace):
    """Instala num repositório git de verdade e roda o gate instalado."""

    def git(self, repo, *args, input_msg=None):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                           cwd=repo, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def setUp(self):
        super().setUp()
        d = example()
        d["packs"]["enabled"] = ["core", "quality-gates"]
        self.repo = self.target("repo")
        code, out = self.run_cli("install", self.briefing(d), self.repo)
        self.assertEqual(code, 0, out)
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-m", "chore: base")
        self.base = self.git(self.repo, "rev-parse", "HEAD").strip()

    def commit(self, msg):
        (self.repo / "f.txt").write_text(msg, encoding="utf-8")
        self.git(self.repo, "add", "f.txt")
        self.git(self.repo, "commit", "-m", msg)

    def gate(self, *args):
        r = subprocess.run([sys.executable, str(self.repo / "scripts" / "sdd" / "gate.py"), *args],
                           capture_output=True, text=True, encoding="utf-8")
        return r.returncode, r.stdout + r.stderr

    def test_good_history_passes_bad_fails(self):
        self.commit("feat(EXM-100): primeira")
        self.assertEqual(self.gate("commits", "--range", f"{self.base}..HEAD")[0], 0)
        self.commit("adiciona coisa sem tipo")
        code, out = self.gate("commits", "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("REPROVADO", out)

    def test_agent_trailer_fails_when_rule_on(self):
        self.commit("feat(EXM-100): x\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        code, out = self.gate("commits", "--range", f"{self.base}..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("atribuição", out)

    def test_branch_names(self):
        self.assertEqual(self.gate("branch", "--branch", "feature/EXM-5-t01-algo")[0], 0)
        self.assertEqual(self.gate("branch", "--branch", "minha-branch")[0], 1)
        self.assertEqual(self.gate("branch", "--branch", "main")[0], 0)

    def test_gaps_report_only_unless_strict(self):
        spec = self.repo / "docs" / "sdd" / "modulos"
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "pagamento.md").write_text("regra X\n⚠️ A DEFINIR: timeout\n", encoding="utf-8")
        code, out = self.gate("gaps")
        self.assertEqual(code, 0)
        self.assertIn("1 lacuna", out)
        self.assertEqual(self.gate("gaps", "--strict")[0], 1)

    def test_installed_files_for_this_host(self):
        for rel in (".claude/settings.json", ".github/workflows/tioh-gate.yml", "docs/sdd/GATES.md"):
            self.assertTrue((self.repo / rel).is_file(), rel)
        self.assertFalse((self.repo / ".gitlab").exists())
        cfg = json.loads((self.repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash(git push:*)", cfg["permissions"]["ask"])
        self.assertIn("Bash(git push --force:*)", cfg["permissions"]["deny"])
        self.assertEqual(cfg["attribution"], {"commit": "", "pr": ""})


class Hashing(unittest.TestCase):
    def test_text_hash_ignores_line_endings_but_binary_does_not(self):
        self.assertEqual(tioh.sha(b"a\r\nb\r\n"), tioh.sha(b"a\nb\n"))
        self.assertNotEqual(tioh.sha(b"a\0\r\nb"), tioh.sha(b"a\0\nb"))
        self.assertIn(tioh.sha_raw(b"x\r\n"), tioh.sha_any(b"x\r\n"))   # manifestos antigos (hash cru) continuam valendo


if __name__ == "__main__":
    unittest.main()
