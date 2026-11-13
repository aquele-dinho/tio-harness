#!/usr/bin/env python3
"""Gera rascunhos de posts (LinkedIn, X e build log) a partir dos commits de um push na branch principal.

Nada é publicado aqui: o script só escreve um markdown para revisão humana (o workflow o transforma numa issue).

Decide se vale postar:
- posta quando a versão (VERSION em bin/tioh.py, configurável) muda no intervalo, ou quando um commit traz `[post]`;
- não posta quando um commit traz `[skip post]`/`[no post]`;
- `--force` ignora o critério (uso manual).

Privacidade: termos de `SOCIAL_DENYLIST` (vírgula ou quebra de linha; guarde como secret) são trocados por
`[redacted]` antes de qualquer envio ao modelo, e o texto gerado é conferido de novo; se algum termo aparecer, o
rascunho é bloqueado. E-mails também são removidos.

Com `ANTHROPIC_API_KEY` definido e o pacote `anthropic` instalado, o texto vem do Claude; sem isso, sai um
esqueleto a partir de um template, para completar à mão.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZERO_SHA = "0" * 40
DEFAULT_VERSION_FILE = "bin/tioh.py"
DEFAULT_VERSION_REGEX = r'^VERSION\s*=\s*"([^"]+)"'
DEFAULT_MODEL = "claude-opus-5-5"
X_LIMIT = 280
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SKIP_RE = re.compile(r"\[(skip|no)[ -]post\]", re.IGNORECASE)
FORCE_RE = re.compile(r"\[post\]", re.IGNORECASE)


@dataclass
class Commit:
    sha: str
    subject: str
    body: str


@dataclass
class Drafts:
    linkedin: str
    x: str
    build_log: str
    source: str  # "claude" ou "template"
    warnings: list[str] = field(default_factory=list)


# ---------- git ----------

def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def commits_in_range(repo: Path, before: str | None, after: str) -> list[Commit]:
    if before and before != ZERO_SHA and _exists(repo, before):
        rev = [f"{before}..{after}"]
    else:
        rev = ["-1", after]  # push inicial ou histórico reescrito: só o último commit
    raw = git(repo, "log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", *rev)
    commits = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, subject, body = (rec.split("\x1f") + ["", ""])[:3]
        commits.append(Commit(sha.strip(), subject.strip(), body.strip()))
    return commits


def _exists(repo: Path, rev: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}^{{commit}}"],
                          capture_output=True).returncode == 0


def version_at(repo: Path, rev: str | None, path: str, regex: str) -> str | None:
    if not rev or rev == ZERO_SHA or not _exists(repo, rev):
        return None
    try:
        text = git(repo, "show", f"{rev}:{path}")
    except RuntimeError:
        return None
    m = re.search(regex, text, re.MULTILINE)
    return m.group(1) if m else None


# ---------- decisão e privacidade ----------

def should_post(commits: list[Commit], old_ver: str | None, new_ver: str | None, force: bool) -> tuple[bool, str]:
    if force:
        return True, "execução manual (--force)"
    if not commits:
        return False, "nenhum commit novo"
    text = "\n".join(f"{c.subject}\n{c.body}" for c in commits)
    if SKIP_RE.search(text):
        return False, "commit pediu [skip post]"
    if new_ver and old_ver and new_ver != old_ver:
        return True, f"versão {old_ver} → {new_ver}"
    if FORCE_RE.search(text):
        return True, "commit pediu [post]"
    return False, "sem mudança de versão nem [post]"


def load_denylist(raw: str | None) -> list[str]:
    if not raw:
        return []
    return sorted({t.strip() for t in re.split(r"[,\n]", raw) if t.strip()}, key=len, reverse=True)


def _term_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def scrub(text: str, denylist: list[str]) -> str:
    text = EMAIL_RE.sub("[email]", text)
    for term in denylist:
        text = _term_re(term).sub("[redacted]", text)
    return text


def leaks(text: str, denylist: list[str]) -> list[str]:
    return [t for t in denylist if _term_re(t).search(text)]


# ---------- geração ----------

SCHEMA = {
    "type": "object",
    "properties": {
        "linkedin": {"type": "string"},
        "x": {"type": "string"},
        "build_log": {"type": "string"},
    },
    "required": ["linkedin", "x", "build_log"],
    "additionalProperties": False,
}


def commits_digest(commits: list[Commit]) -> str:
    parts = []
    for c in commits:
        parts.append(f"- {c.subject}" + (f"\n  {c.body.replace(chr(10), chr(10) + '  ')}" if c.body else ""))
    return "\n".join(parts)


def draft_with_claude(project: str, version: str | None, digest: str, lang: str, model: str) -> Drafts:
    import anthropic  # opcional: só quando há chave

    client = anthropic.Anthropic()
    system = (HERE / "voice.md").read_text(encoding="utf-8")
    user = (
        f"Projeto: {project}\nVersão: {version or 'sem versão'}\nIdioma dos posts: {lang}\n\n"
        f"Commits deste push (podem estar em português; escreva os posts no idioma pedido):\n{digest}"
    )
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("o modelo recusou gerar o rascunho")
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    return Drafts(data["linkedin"].strip(), data["x"].strip(), data["build_log"].strip(), "claude")


def draft_with_template(project: str, version: str | None, commits: list[Commit], lang: str) -> Drafts:
    ver = f" v{version}" if version else ""
    bullets = "\n".join(f"• {c.subject}" for c in commits[:6])
    if lang.lower().startswith("pt"):
        linkedin = (f"{project}{ver} saiu.\n\nO que mudou:\n{bullets}\n\n"
                    "O aprendizado: [escreva aqui a lição e o ângulo de arquitetura]\n\n"
                    "Próximo passo: [complete]\n\n#AIEngineering #SoftwareArchitecture #DevTools")
        x = f"{project}{ver} saiu. [lição em uma frase] #AIEngineering"
        log = f"**{project}{ver}**: [resuma em 2 a 4 frases]"
    else:
        linkedin = (f"{project}{ver} is out.\n\nWhat changed:\n{bullets}\n\n"
                    "The lesson: [write the lesson and the architecture angle]\n\n"
                    "Next: [fill in]\n\n#AIEngineering #SoftwareArchitecture #DevTools")
        x = f"{project}{ver} is out. [one-line lesson] #AIEngineering"
        log = f"**{project}{ver}**: [summarise in 2 to 4 sentences]"
    return Drafts(linkedin, x, log, "template", ["Esqueleto sem IA: complete os trechos entre colchetes."])


def check_drafts(d: Drafts, denylist: list[str]) -> Drafts:
    found = sorted(set(leaks(d.linkedin, denylist) + leaks(d.x, denylist) + leaks(d.build_log, denylist)))
    if found:
        # Não reproduz o termo vazado: a issue pode ser vista por colaboradores.
        d = Drafts("[bloqueado]", "[bloqueado]", "[bloqueado]", d.source,
                   [f"Rascunho bloqueado: {len(found)} termo(s) da denylist apareceram no texto gerado."])
    if len(d.x) > X_LIMIT:
        d.warnings.append(f"O post do X tem {len(d.x)} caracteres (limite {X_LIMIT}).")
    return d


# ---------- saída ----------

def render(project: str, version: str | None, reason: str, d: Drafts, commits: list[Commit],
           denylist: list[str]) -> str:
    warn = "".join(f"> ⚠️ {w}\n" for w in d.warnings)
    listed = "\n".join(f"- `{c.sha[:7]}` {scrub(c.subject, denylist)}" for c in commits) or "- (nenhum)"
    return f"""Rascunhos gerados ({d.source}) para **{project}{' v' + version if version else ''}**. Motivo: {reason}.
Nada foi publicado. Revise, ajuste e publique à mão; depois feche esta issue.

{warn}
## LinkedIn

```text
{d.linkedin}
```

## X ({len(d.x)}/{X_LIMIT})

```text
{d.x}
```

## Build log (tiodati.dev)

```markdown
{d.build_log}
```

## Checklist

- [ ] Nenhum nome de cliente, empregador, pessoa ou ID interno
- [ ] Números e fatos conferem com os commits
- [ ] Publicado no LinkedIn
- [ ] Publicado no X

<details><summary>Commits considerados ({len(commits)})</summary>

{listed}

</details>
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=".", help="repositório git (padrão: .)")
    p.add_argument("--before", help="SHA anterior ao push (github.event.before)")
    p.add_argument("--after", default="HEAD", help="SHA do push (github.sha)")
    p.add_argument("--project", default=os.environ.get("SOCIAL_PROJECT", "Tio Harness"))
    p.add_argument("--lang", default=os.environ.get("SOCIAL_LANG", "en"))
    p.add_argument("--version-file", default=os.environ.get("SOCIAL_VERSION_FILE", DEFAULT_VERSION_FILE))
    p.add_argument("--version-regex", default=os.environ.get("SOCIAL_VERSION_REGEX", DEFAULT_VERSION_REGEX))
    p.add_argument("--force", action="store_true", help="gera mesmo sem mudança de versão")
    p.add_argument("--no-llm", action="store_true", help="usa só o template, sem chamar o modelo")
    p.add_argument("--out", help="arquivo markdown de saída (padrão: stdout)")
    a = p.parse_args(argv)

    repo = Path(a.repo)
    denylist = load_denylist(os.environ.get("SOCIAL_DENYLIST"))
    commits = commits_in_range(repo, a.before, a.after)
    old_ver = version_at(repo, a.before, a.version_file, a.version_regex)
    new_ver = version_at(repo, a.after, a.version_file, a.version_regex)
    ok, reason = should_post(commits, old_ver, new_ver, a.force)

    outputs = {"should_post": str(ok).lower(), "reason": reason,
               "title": f"Post: {a.project}" + (f" v{new_ver}" if new_ver else "")}
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not ok:
        print(f"Sem post: {reason}.")
        if gh_out:
            _write_outputs(gh_out, outputs)
        return 0

    digest = scrub(commits_digest(commits), denylist)
    use_llm = not a.no_llm and os.environ.get("ANTHROPIC_API_KEY")
    drafts = None
    if use_llm:
        try:
            drafts = draft_with_claude(a.project, new_ver, digest, a.lang,
                                       os.environ.get("SOCIAL_MODEL", DEFAULT_MODEL))
        except Exception as e:  # rascunho nunca derruba o push; cai no template
            print(f"Falha ao gerar com o modelo ({type(e).__name__}); usando template.", file=sys.stderr)
    if drafts is None:
        drafts = draft_with_template(a.project, new_ver, commits, a.lang)
        drafts.linkedin, drafts.x = scrub(drafts.linkedin, denylist), scrub(drafts.x, denylist)
    drafts = check_drafts(drafts, denylist)

    md = render(a.project, new_ver, reason, drafts, commits, denylist)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    if gh_out:
        _write_outputs(gh_out, outputs)
    return 0


def _write_outputs(path: str, values: dict[str, str]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


if __name__ == "__main__":
    sys.exit(main())
