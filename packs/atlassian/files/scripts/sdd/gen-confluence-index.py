#!/usr/bin/env python3
"""
Gera docs/sdd/confluence-index.md — índice de IDs/títulos/versões do Confluence oficial.

NÃO espelha conteúdo (AGENTS.md §10): grava apenas ponteiros (ID, tipo, título, versão,
data da última edição, link). O campo `versão` serve para detectar drift entre a wiki e
os artefatos locais sem precisar reler as páginas.

Credencial: lida de ~/.config/atlassian-mcp/rest.authorization.header (header HTTP pronto)
ou da env ATLASSIAN_REST_AUTHORIZATION. Nunca é impressa.
Regenerar com: ~/.config/atlassian-mcp/regen-headers.sh

Uso:
    python3 scripts/sdd/gen-confluence-index.py [-o docs/sdd/confluence-index.md]
"""
import argparse
import datetime as _dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atlassian_rest import BASE, CFG, _auth, _require_base   # noqa: E402

# Raízes indexadas: atlassian.index_roots no briefing; sem elas, a pasta raiz oficial.
ROOTS = [(str(r["id"]), r.get("type", "page"), r.get("label", str(r["id"])))
         for r in (CFG.get("index_roots") or [])] or (
    [(str(CFG["root_folder_id"]), "folder", "Pasta oficial (AGENTS.md §2)")] if CFG.get("root_folder_id") else [])
PROJECT_NAME = CFG["_project"].get("name", "projeto")

CHILD_SEGMENT = {"folder": "folders", "page": "pages"}

_require_base()
AUTH = _auth()


def get(path: str) -> dict:
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(
        url, headers={"Authorization": AUTH, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def direct_children(node_id: str, node_type: str) -> list:
    seg = CHILD_SEGMENT.get(node_type)
    if not seg:
        return []  # slide/embed/whiteboard/database não têm filhos navegáveis
    out, nxt = [], f"/wiki/api/v2/{seg}/{node_id}/direct-children?limit=250"
    while nxt:
        try:
            data = get(nxt)
        except urllib.error.HTTPError as err:
            print(f"  !! {node_id} ({node_type}): HTTP {err.code}", file=sys.stderr)
            return out
        out.extend(data.get("results", []))
        nxt = data.get("_links", {}).get("next")
    return out


def meta(node_id: str) -> dict:
    """Versão e data de edição via API v1, que cobre todos os tipos de conteúdo."""
    try:
        d = get(f"/wiki/rest/api/content/{node_id}?expand=version")
    except urllib.error.HTTPError:
        return {}
    v = d.get("version") or {}
    when = (v.get("when") or "")[:10]
    return {
        "version": v.get("number"),
        "updated": when,
        "webui": (d.get("_links") or {}).get("webui", ""),
    }


def walk(node_id, node_type, title, depth, acc, seen):
    if node_id in seen:
        return
    seen.add(node_id)
    acc.append(
        {"id": node_id, "type": node_type, "title": title, "depth": depth, **meta(node_id)}
    )
    if depth >= 12:
        return
    for c in direct_children(node_id, node_type):
        walk(c["id"], c.get("type", "?"), c.get("title", ""), depth + 1, acc, seen)


TYPE_LABEL = {
    "folder": "📁",
    "page": "📄",
    "slide": "🖼",
    "whiteboard": "🧩",
    "embed": "🔗",
    "database": "🗃",
}


def render(sections: list) -> str:
    today = _dt.date.today().isoformat()
    total = sum(len(nodes) for _, nodes in sections)
    out = [
        f"# Índice do Confluence — {PROJECT_NAME}",
        "",
        f"> Gerado por `scripts/sdd/gen-confluence-index.py` em **{today}**. "
        f"{total} nós indexados.",
        "> Regenerar após qualquer sincronia: "
        "`python3 scripts/sdd/gen-confluence-index.py`.",
        "",
        "**Para que serve:** localizar a fonte oficial por ID sem depender de "
        "`docs/.archive/` (backup histórico, proibido como fonte viva — AGENTS.md §2/§10) "
        "e detectar *drift* comparando a coluna `Ver.` com a versão citada nos artefatos "
        "locais. Este arquivo guarda **apenas ponteiros**, nunca conteúdo — o espelhamento "
        "da wiki no Git é vedado (AGENTS.md §10).",
        "",
    ]
    for label, nodes in sections:
        out += [f"## {label}", ""]
        if not nodes:
            out += ["_Sem nós próprios: já indexado como descendente de uma raiz anterior._", ""]
            continue
        out += ["| | ID | Título | Ver. | Atualizado |", "|---|---|---|---|---|"]
        for n in nodes:
            indent = "&nbsp;" * (n["depth"] * 2)
            icon = TYPE_LABEL.get(n["type"], "•")
            link = n.get("webui") or ""
            title = n["title"].replace("|", "\\|")
            cell = (
                f"[{title}]({BASE}/wiki{link})" if link else title
            )
            out.append(
                f"| {indent}{icon} | `{n['id']}` | {cell} | "
                f"{n.get('version') or '—'} | {n.get('updated') or '—'} |"
            )
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="docs/sdd/confluence-index.md")
    args = ap.parse_args()
    if not ROOTS:
        sys.exit("sem raízes: defina atlassian.index_roots ou atlassian.root_folder_id no briefing")

    sections, seen = [], set()
    for root_id, root_type, label in ROOTS:
        acc = []
        try:
            root = get(f"/wiki/api/v2/{CHILD_SEGMENT[root_type]}/{root_id}")
        except urllib.error.HTTPError as err:
            print(f"!! raiz {root_id} inacessível: HTTP {err.code}", file=sys.stderr)
            continue
        walk(root_id, root_type, root.get("title", root_id), 0, acc, seen)
        sections.append((label, acc))
        print(f"  {label}: {len(acc)} nós", file=sys.stderr)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(sections), encoding="utf-8")
    print(f"escrito: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
