#!/usr/bin/env python3
"""Aplica substituições pontuais ("de" → "para") no storage de uma página do Confluence.

Para páginas editadas à mão, em que republicar a página
inteira a partir de Markdown apagaria o conteúdo que não vive no Git.

Sem --apply é dry-run: confere tudo e mostra o antes/depois de cada trecho em texto; --saida grava
também o storage resultante. Publicar é write em página oficial e exige gate humano (AGENTS.md §10).

Patch (JSON):
    {"pageId": "<id>", "baseVersion": 34, "mensagem": "...",
     "substituicoes": [{"trecho": "rótulo curto", "de": "<storage>", "para": "<storage>"}]}

- "de" e "para" em storage XHTML, com acentos em UTF-8: as entidades nomeadas da página
  (&ccedil;, &atilde;, ...) são convertidas antes da busca; &lt; &gt; &amp; &quot; &apos; ficam.
- Cada "de" precisa ocorrer exatamente uma vez; senão, aborta sem escrever.
- A versão atual da página precisa ser a baseVersion; senão, CONFLITO (alguém editou depois).
- Log: docs/sdd/analises/sync-confluence.log (só com --apply).

Uso:
    python3 scripts/sdd/patch-page.py docs/sdd/analises/patch-cenarios-v35.json
    python3 scripts/sdd/patch-page.py docs/sdd/analises/patch-cenarios-v35.json --apply
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from atlassian_rest import LOG_DIR, call         # noqa: E402
from md2storage import validate                  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
LOG = os.path.join(LOG_DIR, 'sync-confluence.log')
XML_ENT = {'lt', 'gt', 'amp', 'quot', 'apos'}


def normalize(storage):
    return re.sub(r'&([A-Za-z][A-Za-z0-9]*);',
                  lambda m: m.group(0) if m.group(1) in XML_ENT else html.unescape(m.group(0)), storage)


def text(fragment):
    t = re.sub(r'</(p|li|tr|h\d)>', '\n', fragment)
    t = re.sub(r'</t[dh]>', ' | ', t)
    return html.unescape(re.sub(r'<[^>]+>|<[^>]*$', '', t)).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('patch', help='arquivo JSON do patch')
    ap.add_argument('--apply', action='store_true', help='publica de fato (sem isso é dry-run)')
    ap.add_argument('--saida', help='grava o storage resultante neste arquivo (para inspeção)')
    a = ap.parse_args()

    p = json.load(open(a.patch, encoding='utf-8'))
    pid, base = str(p['pageId']), int(p['baseVersion'])
    st, d = call('GET', f'/wiki/api/v2/pages/{pid}', params={'body-format': 'storage'})
    if st != 200:
        sys.exit(f'GET da página {pid} falhou (HTTP {st}): {d}')
    cur = d['version']['number']
    if cur != base:
        sys.exit(f'CONFLITO: a página {pid} está na v{cur}, o patch foi escrito sobre a v{base} — '
                 'releia a página e refaça o patch')

    body = normalize(d['body']['storage']['value'])
    for i, s in enumerate(p['substituicoes'], 1):
        n = body.count(s['de'])
        if n != 1:
            sys.exit(f'[{i}] {s["trecho"]}: "de" ocorre {n} vez(es) na v{cur} — esperava 1; nada foi escrito')
        body = body.replace(s['de'], s['para'])
        print(f'\n=== [{i}] {s["trecho"]}\n--- antes\n{text(s["de"])}\n+++ depois\n{text(s["para"])}')
    validate(body)

    if a.saida:
        with open(a.saida, 'w', encoding='utf-8') as f:
            f.write(body)
    print(f'\n{pid} "{d["title"]}": v{cur} → v{cur + 1}, {len(p["substituicoes"])} trecho(s)'
          + (f', storage em {a.saida}' if a.saida else ''))
    if not a.apply:
        print('DRY-RUN — nada foi escrito. Publicar: --apply (gate humano).')
        return

    st, r = call('PUT', f'/wiki/api/v2/pages/{pid}', {
        'id': pid, 'status': 'current', 'title': d['title'],
        'body': {'representation': 'storage', 'value': body},
        'version': {'number': cur + 1, 'message': p['mensagem']}})
    sha = subprocess.run(['git', '-C', ROOT, 'rev-parse', '--short', 'HEAD'],
                         capture_output=True, text=True).stdout.strip()
    stamp = f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{sha}\tpatch\t{pid}\t{d['title']}"
    if st != 200:
        line = f'{stamp}\tERRO HTTP {st}\t{str(r)[:300]}'
    else:
        _, v = call('GET', f'/wiki/api/v2/pages/{pid}', params={'body-format': 'view'})
        erros = len(re.findall(r'(?i)unknown macro|error rendering|conf-macro-error', v['body']['view']['value']))
        line = (f'{stamp}\tv{cur} → v{r["version"]["number"]}\t{os.path.relpath(a.patch, ROOT)}\t'
                f'{"OK" if not erros else f"ATENÇÃO: {erros} erro(s) de macro na renderização"}')
    print(line)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    if st != 200:
        sys.exit(1)


if __name__ == '__main__':
    main()
