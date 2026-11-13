"""Testes do gerador de rascunhos de posts (tools/social/draft_posts.py). Só stdlib, sem rede."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("draft_posts", ROOT / "tools" / "social" / "draft_posts.py")
dp = importlib.util.module_from_spec(_spec)
sys.modules["draft_posts"] = dp
_spec.loader.exec_module(dp)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "commit.gpgsign", "false")
        self.base = self.commit("0.1.0", "chore: início")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, version, msg):
        f = self.repo / "bin" / "tioh.py"
        f.parent.mkdir(exist_ok=True)
        f.write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        (self.repo / "n.txt").write_text(msg, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", msg)
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    def run_main(self, before, after, *extra, env=None):
        out = self.repo / "out.md"
        gh = self.repo / "gh.txt"
        gh.write_text("", encoding="utf-8")
        old = dict(os.environ)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["GITHUB_OUTPUT"] = str(gh)
        os.environ.update(env or {})
        try:
            dp.main(["--repo", str(self.repo), "--before", before, "--after", after, "--out", str(out), *extra])
        finally:
            os.environ.clear()
            os.environ.update(old)
        outputs = dict(line.split("=", 1) for line in gh.read_text(encoding="utf-8").splitlines())
        return outputs, (out.read_text(encoding="utf-8") if out.exists() else "")

    def test_posta_quando_versao_muda(self):
        head = self.commit("0.2.0", "feat: eval novo")
        outputs, md = self.run_main(self.base, head)
        self.assertEqual(outputs["should_post"], "true")
        self.assertIn("0.1.0 → 0.2.0", outputs["reason"])
        self.assertIn("v0.2.0", outputs["title"])
        self.assertIn("## LinkedIn", md)
        self.assertIn("feat: eval novo", md)

    def test_nao_posta_sem_versao_nova(self):
        head = self.commit("0.1.0", "fix: detalhe")
        outputs, md = self.run_main(self.base, head)
        self.assertEqual(outputs["should_post"], "false")
        self.assertEqual(md, "")

    def test_marcadores_post_e_skip(self):
        head = self.commit("0.1.0", "docs: guia [post]")
        self.assertEqual(self.run_main(self.base, head)[0]["should_post"], "true")
        head2 = self.commit("0.3.0", "feat: algo [skip post]")
        self.assertEqual(self.run_main(head, head2)[0]["should_post"], "false")

    def test_force_e_push_inicial(self):
        head = self.commit("0.1.0", "fix: nada")
        outputs, _ = self.run_main(dp.ZERO_SHA, head, "--force")
        self.assertEqual(outputs["should_post"], "true")

    def test_denylist_remove_nomes(self):
        head = self.commit("0.2.0", "feat: piloto na AcmeCorp com contato joao@acme.example")
        _, md = self.run_main(self.base, head, env={"SOCIAL_DENYLIST": "AcmeCorp, Outra"})
        self.assertNotIn("AcmeCorp", md)
        self.assertNotIn("joao@acme.example", md)
        self.assertIn("[redacted]", md)


class PrivacyTests(unittest.TestCase):
    def test_scrub_respeita_fronteira_de_palavra(self):
        deny = dp.load_denylist("Porto\nAcme")
        self.assertEqual(dp.scrub("migrei o porto e a Acme", deny), "migrei o [redacted] e a [redacted]")
        self.assertEqual(dp.scrub("Portobello e Acmeville", deny), "Portobello e Acmeville")

    def test_vazamento_bloqueia_rascunho(self):
        d = dp.Drafts("Trabalhei com a Acme", "ok", "ok", "claude")
        checked = dp.check_drafts(d, ["Acme"])
        self.assertEqual(checked.linkedin, "[bloqueado]")
        self.assertTrue(any("bloqueado" in w for w in checked.warnings))
        self.assertNotIn("Acme", " ".join(checked.warnings))

    def test_aviso_de_limite_do_x(self):
        d = dp.check_drafts(dp.Drafts("a", "x" * 300, "b", "template"), [])
        self.assertTrue(any("300" in w for w in d.warnings))


if __name__ == "__main__":
    unittest.main()
