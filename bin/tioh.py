#!/usr/bin/env python3
"""tioh — Tio Harness.

Instala o harness agêntico SDD (AGENTS.md, prompts, skills, comandos por agente, scripts) num
projeto, a partir de um briefing TOML. Só usa a biblioteca padrão (Python 3.11+).

Uso:
    python3 bin/tioh.py init   <briefing.toml>                 # cria um briefing em branco
    python3 bin/tioh.py validate <briefing.toml>               # confere campos e lacunas
    python3 bin/tioh.py packs                                  # lista os packs disponíveis
    python3 bin/tioh.py install <briefing.toml> <destino> [--dry-run] [--force] [--prune]
    python3 bin/tioh.py check  <destino> [--briefing <toml>]   # drift; sai 1 se houver
    python3 bin/tioh.py accept <destino> <arquivo>...          # registra customização local intencional
"""
import argparse
import copy
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

VERSION = "0.6.1"
ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = ROOT / "packs"
BRIEFING_TEMPLATE = ROOT / "briefing" / "briefing.template.toml"
STATE_DIR = ".ai-framework"
MANIFEST = f"{STATE_DIR}/manifest.json"
PENDING_MARK = "A DEFINIR"
IGNORED_DIRS = {"__pycache__", "node_modules", ".pylib"}

REQUIRED = [
    "project.name", "project.slug", "project.description", "project.output_language",
    "sources.primary.title", "sdd.spec_dir", "sdd.unit", "agents.targets", "packs.enabled",
]

DEFAULTS = {
    "project": {"organization": "", "repo_kind": "especificação + implementação", "objectives": []},
    "sources": {"archive_dirs": [], "precedence": [], "documents": []},
    "architecture": {"principles": [], "containers": [], "adr_patterns": [], "directives": [], "authority": "",
                     "proposals_dir": "docs/sdd/arquitetura"},
    "stack": {"setup_cmds": [], "standards": [], "hexagonal": {
        "business_dir": "business", "port_dir": "business/port",
        "adapter_dirs": ["controller", "events", "repository", "<integracao>"],
        "usecase_suffix": "Business", "forbidden_dirs": ["api", "application", "domain", "infrastructure"]}},
    "repos": [],
    "domain": {"invariants": [], "glossary": [], "state_machines": []},
    "phasing": {"notes": []},
    "sdd": {"pending_prefix": "P", "modules": [], "pending": [], "api_guidelines": [], "security": [],
            "observability": [], "resilience": [], "test_strategy": [], "review_extra": []},
    "vcs": {"host": "gitlab", "base_branch": "developer", "branch_pattern": "feature/<CHAVE>-<tNN>-<slug-curto>",
            "commit_convention": "Conventional Commits (feat(<CHAVE>): …)"},
    "atlassian": {"status_in_progress": "Em Desenvolvimento", "labels": ["sdd"], "cancel_status": "Cancelado",
                  "mirror": {"enabled": False, "parent": "", "include": ["docs/**/*.md"],
                             "exclude": ["docs/sdd/CONFLUENCE-SYNC.md", "docs/sdd/TEMPLATE-spec.md",
                                         "docs/sdd/prompts/*", "docs/sdd/templates/*",
                                         "docs/sdd/confluence-index.md"],
                             "exclude_extra": [], "map": "docs/sdd/confluence-mirror.json", "labels": [],
                             "mermaid_md2html": "", "mermaid_browser": ""}},
    "meetings": {"transcripts_dir": "", "notes_dir": "docs/sdd/atas", "mention": [], "publish": ""},
    "rules": {"no_agent_attribution": True, "extra": []},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def with_defaults(data):
    d = _merge(DEFAULTS, data)
    d["project"].setdefault("chat_language", d["project"].get("output_language", "pt-BR"))
    d["repos"] = [r for r in d["repos"] if r.get("path")]
    d["domain"]["glossary"] = [g for g in d["domain"]["glossary"] if g.get("term")]
    d["architecture"]["directives"] = [x for x in d["architecture"]["directives"] if x.get("title")]
    d["meetings"]["mention"] = [x for x in d["meetings"]["mention"] if x.get("name")]
    mirror = d["atlassian"]["mirror"]
    if d["meetings"].get("publish") == "jira":   # atas vão para o item do Jira, não para o espelho
        mirror["exclude"] = mirror["exclude"] + [f'{d["meetings"]["notes_dir"].rstrip("/")}/*']
    mirror["exclude"] = mirror["exclude"] + [f"{x.rstrip('/')}/*" for x in d["sources"]["archive_dirs"]
                                             if f"{x.rstrip('/')}/*" not in mirror["exclude"]]
    return d


# --------------------------------------------------------------------------- template engine
class TemplateError(Exception):
    pass


_BLOCK = r"(?:if\s[^@]+|else|endif|for\s[^@]+|endfor)"
_STANDALONE = re.compile(rf"^[ \t]*(@@{_BLOCK}@@)[ \t]*\r?\n", re.M)
_TAG = re.compile(r"@@((?:(?!@@).)+?)@@")


_MISSING = None   # lista quando `collect_missing` está ativo


def _lookup(scopes, path, strict, where):
    for scope in reversed(scopes):
        cur, ok = scope, True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    if strict:
        if _MISSING is not None:          # modo `validate`: acumula tudo para reportar de uma vez
            _MISSING.append((where, path))
            return ""
        raise TemplateError(f"{where}: variável indefinida '{path}'")
    return None


def _cond(expr, scopes, where):
    expr = expr.strip()
    neg = expr.startswith("not ")
    if neg:
        expr = expr[4:].strip()
    m = re.fullmatch(r'([\w.]+)\s*(==|!=)\s*"([^"]*)"', expr)
    if m:
        val = _lookup(scopes, m.group(1), False, where)
        res = (str(val) == m.group(3)) == (m.group(2) == "==")
    else:
        res = bool(_lookup(scopes, expr, False, where))
    return not res if neg else res


_GAP_VALUE = re.compile(rf"^\s*(?:>\s*)?(?:⚠️?\s*)?{PENDING_MARK}\b")
_GAP_OWNER = re.compile(r"\s*\((?:responde|resp\.?|owner|responsável)\s*:\s*([^)]*)\)\s*$", re.I)


def gap_text(val):
    """Normaliza uma lacuna do briefing para que o nome do responsável não pareça o valor.

    '⚠️ A DEFINIR: chave do Jira (responde: PO)' -> '⚠️ A DEFINIR (valor indefinido, não presumir): chave do Jira; decide: PO'.
    Só valores que SÃO lacuna (começam com a marca) mudam; texto que apenas menciona "A DEFINIR" passa intacto.
    """
    if not isinstance(val, str) or not _GAP_VALUE.match(val):
        return val
    owner = ""
    m = _GAP_OWNER.search(val)
    if m:
        owner, val = m.group(1).strip(), val[:m.start()]
    body = val.strip()
    head, sep, rest = body.partition(PENDING_MARK)
    rest = rest.lstrip(" :—-").rstrip()
    out = f"{head}{PENDING_MARK} (valor indefinido, não presumir)"
    if rest:
        out += f": {rest}"
    if owner:
        out += f"; decide: {owner}"
    return out


def _fmt(val, filt, where, path):
    if val is None:
        raise TemplateError(f"{where}: '{path}' está vazio")
    if filt == "json":
        return json.dumps(val, ensure_ascii=False)
    if filt == "upper":
        return str(val).upper()
    if filt == "lower":
        return str(val).lower()
    if filt:
        raise TemplateError(f"{where}: filtro desconhecido '{filt}'")
    if isinstance(val, list):
        return ", ".join(f"`{gap_text(v)}`" if isinstance(v, str) else str(v) for v in val)
    if isinstance(val, bool):
        return "sim" if val else "não"
    return str(gap_text(val))


def _parse(tokens, i, stop):
    nodes = []
    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "text":
            nodes.append(("text", val))
            i += 1
            continue
        head = val.split(None, 1)[0]
        if head in stop:
            return nodes, i
        if head == "if":
            body, i = _parse(tokens, i + 1, {"else", "endif"})
            other = []
            if tokens[i][1].strip() == "else":
                other, i = _parse(tokens, i + 1, {"endif"})
            nodes.append(("if", val[3:], body, other))
            i += 1
        elif head == "for":
            m = re.fullmatch(r"for\s+(\w+)\s+in\s+([\w.]+)", val.strip())
            if not m:
                raise TemplateError(f"for malformado: '{val}'")
            body, i = _parse(tokens, i + 1, {"endfor"})
            nodes.append(("for", m.group(1), m.group(2), body))
            i += 1
        elif head in ("else", "endif", "endfor"):
            raise TemplateError(f"'{head}' sem bloco de abertura")
        else:
            nodes.append(("var", val.strip()))
            i += 1
    if stop:
        raise TemplateError(f"bloco não fechado (esperava {sorted(stop)})")
    return nodes, i


def _eval(nodes, scopes, where):
    out = []
    for n in nodes:
        if n[0] == "text":
            out.append(n[1])
        elif n[0] == "var":
            path, _, filt = n[1].partition("|")
            out.append(_fmt(_lookup(scopes, path.strip(), True, where), filt.strip(), where, path))
        elif n[0] == "if":
            out.append(_eval(n[2] if _cond(n[1], scopes, where) else n[3], scopes, where))
        elif n[0] == "for":
            seq = _lookup(scopes, n[2], False, where) or []
            for idx, item in enumerate(seq):
                loop = {"index": idx + 1, "first": idx == 0, "last": idx == len(seq) - 1}
                out.append(_eval(n[3], scopes + [{n[1]: item, "loop": loop}], where))
    return "".join(out)


def render(text, ctx, where="<template>"):
    text = _STANDALONE.sub(r"\1", text)
    parts = _TAG.split(text)
    tokens = [("text" if k % 2 == 0 else "tag", p) for k, p in enumerate(parts) if p or k % 2]
    try:
        nodes, _ = _parse(tokens, 0, set())
    except TemplateError as e:
        raise TemplateError(f"{where}: {e}") from None
    return _eval(nodes, [ctx], where)


# --------------------------------------------------------------------------- briefing
def load_briefing(path):
    path = Path(path)
    if not path.is_file():
        sys.exit(f"briefing não encontrado: {path}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw) if path.suffix == ".json" else tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:      # TOMLDecodeError e JSONDecodeError herdam de ValueError
        sys.exit(f"briefing ilegível ({path.name}): {e}")
    if not isinstance(data, dict):
        sys.exit(f"briefing ilegível ({path.name}): a raiz precisa ser uma tabela")
    return data, raw


def _get(d, dotted):
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def _pending_count(node):
    if isinstance(node, str):
        return int(PENDING_MARK in node)
    if isinstance(node, dict):
        return sum(_pending_count(v) for v in node.values())
    if isinstance(node, list):
        return sum(_pending_count(v) for v in node)
    return 0


def validate(data):
    missing = [k for k in REQUIRED if _get(data, k) in (None, "", [])]
    unknown = [p for p in _get(data, "packs.enabled") or [] if isinstance(p, str)
               and not (PACKS_DIR / p / "pack.toml").is_file()]
    return missing, unknown, _pending_count(data)


AGENT_TARGETS = ("copilot", "claude", "gemini", "warp", "codex")
_STR_FIELDS = ["project.name", "project.slug", "project.description", "project.output_language",
               "sources.primary.title", "sdd.spec_dir", "sdd.unit", "vcs.host", "vcs.base_branch"]
_LIST_FIELDS = ["agents.targets", "packs.enabled"]


def _suggest(word, options):
    near = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f" (você quis dizer '{near[0]}'?)" if near else ""


def type_problems(data):
    """Erros de tipo e de valor (bloqueiam a instalação)."""
    errs = []
    for k in _STR_FIELDS:
        v = _get(data, k)
        if v is not None and not isinstance(v, str):
            errs.append(f"{k} deveria ser texto, veio {type(v).__name__}")
    for k in _LIST_FIELDS:
        v = _get(data, k)
        if v is None:
            continue
        if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
            errs.append(f"{k} deveria ser uma lista de textos (ex.: [\"core\"]), veio {v!r}")
    targets = _get(data, "agents.targets")
    if isinstance(targets, list):
        for t in targets:
            if isinstance(t, str) and t not in AGENT_TARGETS:
                errs.append(f"agents.targets: agente desconhecido '{t}'{_suggest(t, AGENT_TARGETS)}; válidos: {', '.join(AGENT_TARGETS)}")
    host = _get(data, "vcs.host")
    if isinstance(host, str) and host not in _VCS:
        errs.append(f"vcs.host: '{host}' desconhecido{_suggest(host, _VCS)}; válidos: {', '.join(_VCS)}")
    return errs


_VAR_REF = re.compile(r"@@\s*(?:if\s+|for\s+\w+\s+in\s+)?(?:not\s+)?([a-z_]+(?:\.[a-z_0-9]+)+)")
_COMMENTED_KEY = re.compile(r"^#\s*([a-z_][a-z_0-9]*)\s*=", re.M)


def known_paths():
    """Caminhos de briefing conhecidos: defaults + modelo (inclusive chaves comentadas) + variáveis usadas nos packs."""
    paths = set()

    def walk(node, pre):
        if isinstance(node, dict):
            for k, v in node.items():
                path = f"{pre}.{k}" if pre else k
                paths.add(path)
                walk(v, path)
        elif isinstance(node, list) and node and isinstance(node[0], dict):
            walk(node[0], pre)

    walk(DEFAULTS, "")
    if BRIEFING_TEMPLATE.is_file():
        text = BRIEFING_TEMPLATE.read_text(encoding="utf-8")
        walk(tomllib.loads(text), "")
        section = ""
        for line in text.splitlines():
            m = re.match(r"^#?\s*\[\[?([a-z_][a-z_0-9.]*)\]\]?", line)
            if m:
                section = m.group(1)
                paths.add(section)
                continue
            m = _COMMENTED_KEY.match(line)
            if m:
                paths.add(f"{section}.{m.group(1)}" if section else m.group(1))
    for f in PACKS_DIR.rglob("*"):
        if f.is_file() and not IGNORED_DIRS & set(f.parts):
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in _VAR_REF.finditer(text):
                parts = m.group(1).split(".")
                for i in range(1, len(parts) + 1):
                    paths.add(".".join(parts[:i]))
    return paths


def unknown_key_warnings(data):
    """Chaves que o framework não conhece (provável erro de digitação). Só avisa: o briefing pode ter extras."""
    known = known_paths()
    warns = []

    def siblings(pre):
        if not pre:
            return {p.split(".")[0] for p in known}
        return {p[len(pre) + 1:].split(".")[0] for p in known if p.startswith(pre + ".")}

    def walk(node, pre):
        if not isinstance(node, dict):
            return
        sibs = siblings(pre)
        if pre and not sibs:               # tabela de forma livre (ex.: mapas): não há o que comparar
            return
        for k, v in node.items():
            path = f"{pre}.{k}" if pre else k
            if path not in known:
                warns.append(f"chave desconhecida '{path}'{_suggest(k, sibs)}")
            else:
                walk(v, path)

    walk(data, "")
    return warns


def collect_missing(fn):
    """Roda `fn` acumulando TODAS as variáveis ausentes em vez de parar na primeira."""
    global _MISSING
    _MISSING = []
    try:
        fn()
        return list(dict.fromkeys(_MISSING))
    finally:
        _MISSING = None


def explain_missing(missing):
    """Traduz (arquivo, variável) em orientação: qual pack precisa de qual campo do briefing."""
    by_var = {}
    for where, path in missing:
        by_var.setdefault(path, set()).add(where.split("/", 1)[0].split(":")[0])
    out = []
    for path, packs in sorted(by_var.items()):
        sec, _, key = path.rpartition(".")
        loc = f"[{sec}] {key}" if sec else path
        out.append(f"falta {loc} (usado por: {', '.join(sorted(packs))})")
    return out


_VCS = {
    "gitlab": {"cli": "glab", "mr": "MR", "mr_long": "Merge Request",
               "view": "glab mr view {id} | cat", "diff": "glab mr diff {id} | cat",
               "create": "glab mr create --source-branch <branch> --target-branch <base> --title \"...\" --description \"...\"",
               "merge": "glab mr merge {id}", "note": "glab mr note {id} --message \"...\""},
    "github": {"cli": "gh", "mr": "PR", "mr_long": "Pull Request",
               "view": "gh pr view {id} | cat", "diff": "gh pr diff {id} | cat",
               "create": "gh pr create --head <branch> --base <base> --title \"...\" --body \"...\"",
               "merge": "gh pr merge {id}", "note": "gh pr comment {id} --body \"...\""},
}


def build_context(data):
    ctx = with_defaults(data)
    ctx.setdefault("framework", {})["version"] = VERSION
    targets = _get(ctx, "agents.targets") or []
    for t in ("copilot", "claude", "gemini", "warp", "codex"):
        ctx["agents"][t] = t in targets
    ctx["has"] = {p.replace("-", "_"): True for p in _get(ctx, "packs.enabled") or []}

    vcs = ctx.setdefault("vcs", {})
    base = _VCS.get(vcs.get("host", "gitlab"), _VCS["gitlab"])
    for k, v in base.items():
        vcs.setdefault(k, v)

    at = ctx.setdefault("atlassian", {})
    proj = at.get("jira_project")
    tracker = ctx.setdefault("tracker", {})
    tracker.setdefault("name", "Jira" if proj else "rastreador de tarefas")
    tracker.setdefault("key", f"{proj}-*" if proj else "<CHAVE>-*")
    tracker.setdefault("key_example", f"{proj}-123" if proj else "<CHAVE>-123")
    tracker.setdefault("status_in_progress", at.get("status_in_progress", "Em Desenvolvimento"))
    ctx.setdefault("docs_platform", "Confluence" if ctx["has"].get("atlassian") else "repositório de documentação")
    ctx.setdefault("repos_impl", [r for r in ctx.get("repos", []) if not r.get("reference")])
    return ctx


# --------------------------------------------------------------------------- packs
def load_pack(name):
    f = PACKS_DIR / name / "pack.toml"
    if not f.is_file():
        sys.exit(f"pack inexistente: {name}")
    meta = tomllib.loads(f.read_text(encoding="utf-8"))
    meta["dir"] = f.parent / "files"
    return meta


def resolve_packs(enabled):
    order, seen = [], set()

    def visit(n, chain):
        if n in seen:
            return
        if n in chain:
            sys.exit(f"dependência circular entre packs: {' -> '.join(chain + [n])}")
        meta = load_pack(n)
        for r in meta.get("requires", []):
            visit(r, chain + [n])
        seen.add(n)
        order.append(meta)

    for n in ["core"] + [p for p in enabled if p != "core"]:
        visit(n, [])
    return order


def plan_files(packs, ctx):
    plan = {}
    for pack in packs:
        conds = pack.get("when", {})
        for src in sorted(pack["dir"].rglob("*")):
            if not src.is_file() or src.suffix == ".pyc" or IGNORED_DIRS & set(src.parts):
                continue
            rel = src.relative_to(pack["dir"]).as_posix()
            if any(rel.startswith(pref) and not _cond(expr, [ctx], f"{pack['name']}:{pref}")
                   for pref, expr in conds.items()):
                continue
            where = f"{pack['name']}/{rel}"
            out = render(rel, ctx, where)
            if out.endswith(".tmpl"):
                out = out[:-5]
                content = render(src.read_text(encoding="utf-8"), ctx, where).encode("utf-8")
            else:
                content = src.read_bytes()
            plan[out] = {"content": content, "pack": pack["name"], "exec": src.suffix in (".sh", ".py")
                         or out.endswith((".sh", ".py"))}
    if ctx["agents"].get("claude"):
        for out in [p for p in plan if p.startswith(".agents/skills/")]:
            plan[".claude/skills/" + out[len(".agents/skills/"):]] = dict(plan[out], mirror=True)
    return plan


def sha_raw(b):
    return hashlib.sha256(b).hexdigest()


def sha(b):
    """SHA-256 com fins de linha normalizados (CRLF → LF) quando o conteúdo é texto: o mesmo arquivo tem o mesmo
    hash no checkout do macOS/Linux (LF) e no do Windows com autocrlf (CRLF), sem falso drift. Binários (com NUL)
    ficam intactos."""
    if b"\r\n" in b and b"\0" not in b:
        b = b.replace(b"\r\n", b"\n")
    return sha_raw(b)


def sha_any(b):
    """Hashes aceitos ao comparar com o manifesto: o normalizado e o dos bytes crus (manifestos de versões
    antigas guardam o hash cru)."""
    return {sha(b), sha_raw(b)}


def classify(target, plan, manifest):
    """Retorna lista de (rel, ação) — create|update|same|custom|conflict.

    custom = arquivo customizado no projeto e aceito (`accept`), sem mudança do framework desde então.
    """
    known = manifest.get("files", {})
    custom = manifest.get("customized", {})
    out = []
    for rel, item in sorted(plan.items()):
        dst = target / rel
        new = sha(item["content"])
        if not dst.exists():
            out.append((rel, "create"))
            continue
        cur = sha_any(dst.read_bytes())
        if new in cur:
            out.append((rel, "same"))
        elif known.get(rel) in cur:
            out.append((rel, "update"))
        elif custom.get(rel) in cur and known.get(rel) in sha_any(item["content"]):
            out.append((rel, "custom"))
        else:
            out.append((rel, "conflict"))
    return out


# --------------------------------------------------------------------------- comandos
def cmd_init(a):
    dst = Path(a.briefing)
    if dst.exists():
        sys.exit(f"{dst} já existe — não sobrescrevo")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BRIEFING_TEMPLATE, dst)
    print(f"briefing criado: {dst}\nPreencha-o (ou use o bootstrap a partir de documento) e rode `validate`.")


def diagnose(data):
    """Todos os problemas do briefing: (erros, avisos, lacunas). Erros bloqueiam; avisos não."""
    missing, unknown, pend = validate(data)
    errs = [f"campo obrigatório ausente: {k}" for k in missing]
    errs += [f"pack '{p}' não existe em packs/{_suggest(p, [d.name for d in PACKS_DIR.iterdir() if d.is_dir()])}" for p in unknown]
    errs += type_problems(data)
    warns = unknown_key_warnings(data)
    if not errs:
        try:
            ctx = build_context(data)
            absent = collect_missing(lambda: plan_files(resolve_packs(_get(data, "packs.enabled") or []), ctx))
            errs += explain_missing(absent)
        except TemplateError as e:
            errs.append(f"template: {e}")
    return errs, warns, pend


def cmd_validate(a):
    data, _ = load_briefing(a.briefing)
    errs, warns, pend = diagnose(data)
    for e in errs:
        print(f"ERRO    {e}")
    for w in warns:
        print(f"AVISO   {w}")
    print(f"Lacunas marcadas '{PENDING_MARK}': {pend}")
    if errs:
        sys.exit(1)
    print("Briefing válido." + (f" ({len(warns)} aviso(s))" if warns else ""))


def cmd_packs(_a):
    for f in sorted(PACKS_DIR.glob("*/pack.toml")):
        m = tomllib.loads(f.read_text(encoding="utf-8"))
        req = f" (requer: {', '.join(m['requires'])})" if m.get("requires") else ""
        print(f"{m['name']:<24} {m.get('description', '')}{req}")


def _load_manifest(target):
    f = target / MANIFEST
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def cmd_install(a):
    data, raw = load_briefing(a.briefing)
    errs, _warns, pend = diagnose(data)
    if errs:
        sys.exit("briefing inválido:\n  - " + "\n  - ".join(errs) + "\nRode `tioh validate <briefing>` para o relatório completo.")
    target = Path(a.target).resolve()
    if not target.is_dir():
        sys.exit(f"destino não existe: {target}")
    if target == ROOT or ROOT in target.parents:
        sys.exit("o destino não pode ser o próprio framework")

    ctx = build_context(data)
    packs = resolve_packs(_get(data, "packs.enabled"))
    plan = plan_files(packs, ctx)
    manifest = _load_manifest(target)
    actions = classify(target, plan, manifest)
    obsolete = sorted(set(manifest.get("files", {})) - set(plan))

    counts = {}
    for rel, act in actions:
        counts[act] = counts.get(act, 0) + 1
        if act not in ("same", "custom"):
            print(f"{act.upper():<9} {rel}" + ("  → gravado como .tioh-new" if act == "conflict" and not a.force else ""))
    for rel in obsolete:
        print(f"OBSOLETO  {rel}" + ("  → removido" if a.prune else "  (use --prune para remover se intocado)"))
    print(f"Resumo: {counts} · packs: {[p['name'] for p in packs]} · lacunas '{PENDING_MARK}' no briefing: {pend}")
    if a.dry_run:
        print("DRY-RUN — nada foi escrito.")
        return

    files = dict(manifest.get("files", {}))
    customized = dict(manifest.get("customized", {}))
    for rel, act in actions:
        item = plan[rel]
        dst = target / rel
        if act in ("same", "custom"):
            files[rel] = sha(item["content"])
            if act == "same":
                customized.pop(rel, None)
            continue
        if act == "conflict" and not a.force:
            dst = dst.with_name(dst.name + ".tioh-new")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(item["content"])
        if item["exec"]:
            dst.chmod(dst.stat().st_mode | 0o111)
        if act != "conflict" or a.force:
            files[rel] = sha(item["content"])
            customized.pop(rel, None)
    for rel in obsolete:
        dst = target / rel
        if a.prune and dst.is_file() and files.get(rel) in sha_any(dst.read_bytes()):
            dst.unlink()
            files.pop(rel, None)

    state = target / STATE_DIR
    state.mkdir(exist_ok=True)
    (state / ("briefing" + Path(a.briefing).suffix)).write_bytes(raw)
    (state / "project.json").write_text(json.dumps(with_defaults(data), ensure_ascii=False, indent=2), encoding="utf-8")
    _write_manifest(target, {
        "framework_version": VERSION,
        "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "briefing_sha256": sha(raw),
        "packs": [p["name"] for p in packs],
        "files": files,
        "customized": customized,
    })
    print(f"Instalado em {target}. Estado em {STATE_DIR}/ (versione-o junto com o projeto).")


def _write_manifest(target, m):
    m["files"] = dict(sorted(m["files"].items()))
    m["customized"] = dict(sorted(m.get("customized", {}).items()))
    (target / MANIFEST).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _plan_for(target, briefing=None):
    briefing = Path(briefing) if briefing else next(iter(sorted((target / STATE_DIR).glob("briefing.*"))), None)
    if not briefing:
        sys.exit(f"sem briefing: passe --briefing ou instale antes (procurei em {STATE_DIR}/)")
    data, _ = load_briefing(briefing)
    return plan_files(resolve_packs(_get(data, "packs.enabled") or []), build_context(data))


def cmd_accept(a):
    """Aceita a versão local (customizada) de arquivos gerenciados: deixam de contar como drift
    até o framework mudar o template deles de novo."""
    target = Path(a.target).resolve()
    manifest = _load_manifest(target)
    if not manifest:
        sys.exit("destino sem manifesto — instale antes")
    plan = _plan_for(target)
    for rel in a.paths:
        rel = Path(rel).as_posix()
        dst = target / rel
        if rel not in plan or not dst.is_file():
            sys.exit(f"{rel}: não é arquivo gerenciado pelo framework neste destino")
        manifest["files"][rel] = sha(plan[rel]["content"])
        manifest.setdefault("customized", {})[rel] = sha(dst.read_bytes())
        pending = dst.with_name(dst.name + ".tioh-new")
        print(f"ACEITO    {rel}" + (f"  (pode apagar {pending.name})" if pending.exists() else ""))
    _write_manifest(target, manifest)


def cmd_check(a):
    target = Path(a.target).resolve()
    plan = _plan_for(target, a.briefing)
    manifest = _load_manifest(target)
    drift = []
    for rel, act in classify(target, plan, manifest):
        if act == "create":
            drift.append(f"AUSENTE    {rel}")
        elif act == "update":
            drift.append(f"DESATUAL.  {rel} (framework mudou; rode install)")
        elif act == "conflict":
            drift.append(f"ALTERADO   {rel} (editado no projeto e/ou no framework; incorporar ao briefing/pack ou `accept`)")
    if manifest.get("framework_version") not in (None, VERSION):
        drift.append(f"VERSÃO     instalado com {manifest['framework_version']}, framework atual {VERSION}")
    if not drift:
        print(f"Harness em dia em {target}.")
        return
    print("\n".join(drift))
    sys.exit(1)


def main():
    for fluxo in (sys.stdout, sys.stderr):   # Windows: console/pipe em cp1252 quebra com acentos e emoji
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="tioh", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="cria um briefing em branco")
    p.add_argument("briefing")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("validate", help="confere o briefing e renderiza em memória")
    p.add_argument("briefing")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("packs", help="lista os packs")
    p.set_defaults(fn=cmd_packs)
    p = sub.add_parser("install", help="instala/atualiza o harness no destino")
    p.add_argument("briefing")
    p.add_argument("target")
    p.add_argument("--dry-run", action="store_true", help="só mostra o plano")
    p.add_argument("--force", action="store_true", help="sobrescreve arquivos editados no projeto")
    p.add_argument("--prune", action="store_true", help="remove arquivos obsoletos ainda intocados")
    p.set_defaults(fn=cmd_install)
    p = sub.add_parser("check", help="detecta drift entre o projeto e o framework")
    p.add_argument("target")
    p.add_argument("--briefing")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("accept", help="aceita a versão local customizada de arquivos gerenciados")
    p.add_argument("target")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_accept)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
