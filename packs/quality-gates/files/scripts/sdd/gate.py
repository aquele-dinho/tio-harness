#!/usr/bin/env python3
"""Gate determinístico do harness (sem LLM, só stdlib).

  gate.py commits [--range A..B]   mensagens: Conventional Commits; sem atribuição de agente se a regra estiver ligada
  gate.py branch  [--branch NOME]  nome da branch segue vcs.branch_pattern
  gate.py gaps    [--strict]       conta lacunas "A DEFINIR" nas specs (--strict: reprova se houver)

Lê a configuração de .ai-framework/project.json. Sai com 1 se algum gate reprovar.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CC_TYPES = "feat|fix|chore|docs|refactor|test|ci|build|perf|style|revert"
SUBJECT = re.compile(rf"^(?:{CC_TYPES})(?:\([^)\s][^)]*\))?!?: \S")
REVERT = re.compile(r'^Revert ".+"$')          # mensagem padrão do `git revert`
_AI = r"(claude|anthropic|copilot|gemini|codex|openai|chatgpt|gpt-?\d|cursor|windsurf|aider|devin)"
AGENT_TRAILER = re.compile(
    rf"(?im)^\s*co-authored-by:.*{_AI}|generated (with|by) \[?{_AI}|🤖")


def config():
    f = ROOT / ".ai-framework" / "project.json"
    if not f.is_file():
        sys.exit(f"sem {f}: rode o gate na raiz de um projeto instalado pelo tioh")
    return json.loads(f.read_text(encoding="utf-8"))


def commit_problems(message, no_attribution=True):
    """Problemas de uma mensagem de commit (lista vazia = ok)."""
    lines = message.strip().splitlines()
    subject = lines[0] if lines else ""
    out = []
    if not (SUBJECT.match(subject) or REVERT.match(subject)):
        out.append(f"assunto fora do padrão Conventional Commits (tipo(escopo): descrição): {subject!r}")
    if no_attribution and AGENT_TRAILER.search(message):
        out.append("atribuição de agente na mensagem (Co-Authored-By/Generated with) — regra do projeto proíbe")
    return out


def branch_regex(pattern):
    """Converte 'feature/<CHAVE>-<tNN>-<slug>' em regex: cada <...> casa um trecho sem '/' nem espaços."""
    parts = re.split(r"(<[^>]*>)", pattern)
    body = "".join(r"[A-Za-z0-9._-]+" if p.startswith("<") and p.endswith(">") else re.escape(p) for p in parts)
    return re.compile(rf"^{body}$")


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        sys.exit(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def cmd_commits(a):
    cfg = config()
    rng = a.range or f"origin/{cfg['vcs']['base_branch']}..HEAD"
    raw = git("log", "--no-merges", "--format=%H%x1f%B%x1e", rng)
    bad = 0
    commits = [c for c in raw.split("\x1e") if c.strip()]
    for c in commits:
        sha, _, msg = c.strip().partition("\x1f")
        for p in commit_problems(msg, cfg.get("rules", {}).get("no_agent_attribution", True)):
            print(f"REPROVADO {sha[:8]}  {p}")
            bad += 1
    print(f"{len(commits)} commit(s) verificados em {rng}: {'REPROVADO' if bad else 'ok'}")
    return 1 if bad else 0


def cmd_branch(a):
    cfg = config()
    vcs = cfg["vcs"]
    name = a.branch or git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if name in (vcs["base_branch"], "HEAD"):
        print(f"branch {name!r}: sem verificação (base ou HEAD destacado)")
        return 0
    if branch_regex(vcs["branch_pattern"]).match(name):
        print(f"branch {name!r}: ok")
        return 0
    print(f"REPROVADO branch {name!r} não segue {vcs['branch_pattern']!r}")
    return 1


def cmd_gaps(a):
    cfg = config()
    spec_dir = ROOT / cfg["sdd"]["spec_dir"]
    total = 0
    for f in sorted(spec_dir.rglob("*.md")) if spec_dir.is_dir() else []:
        n = f.read_text(encoding="utf-8", errors="replace").count("A DEFINIR")
        if n:
            print(f"{n:>3}  {f.relative_to(ROOT).as_posix()}")
            total += n
    print(f"{total} lacuna(s) 'A DEFINIR' nas specs")
    return 1 if (total and a.strict) else 0


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("commits")
    p.add_argument("--range")
    p.set_defaults(fn=cmd_commits)
    p = sub.add_parser("branch")
    p.add_argument("--branch")
    p.set_defaults(fn=cmd_branch)
    p = sub.add_parser("gaps")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(fn=cmd_gaps)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
