#!/usr/bin/env python3
"""Publica specs de sdd.spec_dir nas páginas canônicas do Confluence (CONFLUENCE-SYNC.md §6.3).

Sem --apply é dry-run: mostra o que seria publicado e não escreve nada. Publicar é write em
página oficial e exige gate humano (AGENTS.md §10).

- Mapa spec → página: atlassian.spec_pages no briefing (.ai-framework/project.json). O título da
  página é mantido. A spec é o arquivo <spec_dir>/<id>-*.md.
- Rodapé de rastreio: arquivo, commit, blob (hash do arquivo), data e autor.
- Página cujo rodapé já cita o mesmo blob é ignorada (conteúdo igual); --force republica.
- Arquivo com mudança não commitada é recusado (o rodapé mentiria sobre o commit);
  --allow-dirty publica mesmo assim e marca "(com alterações locais)".
- Rótulos garantidos: atlassian.labels + spec-<id>.
- Log: docs/sdd/analises/sync-confluence.log.

Uso:
    python3 scripts/sdd/publish-specs.py                  # dry-run de todas as specs mapeadas
    python3 scripts/sdd/publish-specs.py 05 11 --apply
    python3 scripts/sdd/publish-specs.py 11 --apply --nota "Recalibração do §9."
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from atlassian_rest import CFG, LOG_DIR, SPEC_DIR, call, myself   # noqa: E402
from md2storage import convert, validate         # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
LOG = os.path.join(LOG_DIR, 'sync-confluence.log')
PAGES = {str(k): str(v) for k, v in (CFG.get('spec_pages') or {}).items()}
LABELS = list(CFG.get('labels') or [])


def git(*args):
    return subprocess.run(['git', '-C', ROOT, *args], capture_output=True, text=True, check=True).stdout.strip()


def esc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('modulos', nargs='*', help='ids das specs, ex.: 01 05 11 (padrão: todas)')
    ap.add_argument('--apply', action='store_true', help='publica de fato (sem isso é dry-run)')
    ap.add_argument('--force', action='store_true', help='republica mesmo com o mesmo blob')
    ap.add_argument('--allow-dirty', action='store_true', help='aceita arquivo com mudança não commitada')
    ap.add_argument('--nota', help='texto de um painel informativo no topo da página')
    ap.add_argument('--mensagem', help='mensagem da versão no Confluence')
    a = ap.parse_args()

    if not PAGES:
        sys.exit('nenhuma spec mapeada: preencha atlassian.spec_pages no briefing e reinstale')
    sha = git('rev-parse', '--short', 'HEAD')
    hoje = datetime.date.today().isoformat()
    autor = myself()['displayName']

    def log(line):
        line = f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{sha}\t{line}"
        print(line)
        if a.apply:
            with open(LOG, 'a') as f:
                f.write(line + '\n')

    for n in a.modulos or sorted(PAGES):
        pid = PAGES[n]
        path = glob.glob(os.path.join(ROOT, SPEC_DIR, f'{n}-*.md'))
        if len(path) != 1:
            sys.exit(f'{n}: esperava 1 arquivo em {SPEC_DIR}/, achei {len(path)}')
        rel = os.path.relpath(path[0], ROOT)
        dirty = bool(git('status', '--porcelain', '--', rel))
        if dirty and not a.allow_dirty:
            sys.exit(f'{n}: {rel} tem mudança não commitada — commite antes ou use --allow-dirty')
        blob = git('hash-object', rel)[:10]

        st, d = call('GET', f'/wiki/api/v2/pages/{pid}', params={'body-format': 'storage'})
        if st != 200:
            sys.exit(f'{n}: GET da página {pid} falhou (HTTP {st}): {d}')
        old = d['version']['number']
        if f'blob {blob}' in d['body']['storage']['value'] and not a.force:
            log(f'{n}\t{pid}\tv{old}\tjá contém o blob {blob} — ignorado')
            continue

        body = ''
        if a.nota:
            body += ('<ac:structured-macro ac:name="info" ac:schema-version="1"><ac:rich-text-body>'
                     f'<p>{esc(a.nota)}</p></ac:rich-text-body></ac:structured-macro>')
        body += convert(open(path[0]).read())
        body += (f'<hr /><p><em>Rastreio: fonte <code>{rel}</code> · commit {sha} (blob {blob})'
                 f'{" (com alterações locais)" if dirty else ""} · sincronizado em {hoje} · '
                 f'autor: {esc(autor)}.</em></p>')
        validate(body)

        if not a.apply:
            log(f'{n}\t{pid}\t{d["title"]}\tv{old} → v{old + 1}\t{len(body)} bytes\tDRY-RUN')
            continue
        st, r = call('PUT', f'/wiki/api/v2/pages/{pid}', {
            'id': pid, 'status': 'current', 'title': d['title'],
            'body': {'representation': 'storage', 'value': body},
            'version': {'number': old + 1,
                        'message': a.mensagem or f'Sincronização de {rel} (commit {sha}).'}})
        if st != 200:
            log(f'{n}\t{pid}\tERRO HTTP {st}\t{str(r)[:300]}')
            sys.exit(1)
        st, lab = call('GET', f'/wiki/api/v2/pages/{pid}/labels')
        have = {x['name'] for x in lab.get('results', [])} if st == 200 else set()
        want = [x for x in (*LABELS, f'spec-{n}') if x not in have]
        if want:
            call('POST', f'/wiki/rest/api/content/{pid}/label', [{'prefix': 'global', 'name': x} for x in want])
        st, v = call('GET', f'/wiki/api/v2/pages/{pid}', params={'body-format': 'view'})
        erros = len(re.findall(r'(?i)unknown macro|error rendering|conf-macro-error', v['body']['view']['value']))
        log(f'{n}\t{pid}\t{d["title"]}\tv{old} → v{r["version"]["number"]}\trótulos +{want or "—"}\t'
            f'{"OK" if not erros else f"ATENÇÃO: {erros} erro(s) de macro na renderização"}')
    print('Lembrete: regenerar o índice — python3 scripts/sdd/gen-confluence-index.py')


if __name__ == '__main__':
    main()
