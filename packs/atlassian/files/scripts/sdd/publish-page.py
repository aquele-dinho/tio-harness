#!/usr/bin/env python3
"""Publica um arquivo Markdown qualquer como página do Confluence (cria ou atualiza).

Complementa publish-specs.py, que só atualiza as páginas mapeadas em atlassian.spec_pages. Mesmas
convenções: sem --apply é dry-run; arquivo com mudança não commitada é recusado; rodapé de
rastreio com arquivo, commit, blob, data e autor; conferência de erros de renderização; log em
docs/sdd/analises/sync-confluence.log. Publicar é write em página oficial e exige gate humano
(AGENTS.md §10).

- Criar:     --parent <id da pasta ou página-mãe>  (título = H1 do arquivo, ou --titulo)
- Atualizar: --page <id da página>                 (título mantido)
- --toc troca a seção "## Sumário" pela macro de sumário do Confluence (as âncoras do Markdown
  não funcionam lá).
- --anexo <arquivo> (repetível) anexa arquivos à página; um anexo de mesmo nome ganha nova versão.

Uso:
    python3 scripts/sdd/publish-page.py docs/sdd/arquitetura/lld-consolidado.md --parent 123456 --toc
    python3 scripts/sdd/publish-page.py docs/sdd/arquitetura/lld-consolidado.md --parent 123456 --toc --apply
    python3 scripts/sdd/publish-page.py docs/sdd/arquitetura/lld-consolidado.md --page 123456 --toc --apply
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from atlassian_rest import BASE, CFG, LOG_DIR, _auth, call, myself, page_url   # noqa: E402
from md2storage import convert, validate         # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
LOG = os.path.join(LOG_DIR, 'sync-confluence.log')
TOC = ('<ac:structured-macro ac:name="toc" ac:schema-version="1">'
       '<ac:parameter ac:name="maxLevel">2</ac:parameter></ac:structured-macro>')


def git(*args):
    return subprocess.run(['git', '-C', ROOT, *args], capture_output=True, text=True, check=True).stdout.strip()


def esc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def anexar(pid, caminho):
    """Envia um anexo (multipart). Se já houver anexo de mesmo nome, publica nova versão dele."""
    nome = os.path.basename(caminho)
    st, ex = call('GET', f'/wiki/rest/api/content/{pid}/child/attachment', params={'filename': nome})
    existente = ex['results'][0]['id'] if st == 200 and ex.get('results') else None
    url = f'{BASE}/wiki/rest/api/content/{pid}/child/attachment' + (f'/{existente}/data' if existente else '')
    fronteira = uuid.uuid4().hex
    corpo = (f'--{fronteira}\r\nContent-Disposition: form-data; name="file"; filename="{nome}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode() + open(caminho, 'rb').read() + \
            f'\r\n--{fronteira}\r\nContent-Disposition: form-data; name="minorEdit"\r\n\r\ntrue\r\n--{fronteira}--\r\n'.encode()
    req = urllib.request.Request(url, data=corpo, method='POST', headers={
        'Authorization': _auth(), 'Accept': 'application/json', 'X-Atlassian-Token': 'no-check',
        'Content-Type': f'multipart/form-data; boundary={fronteira}'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')[:300]


def space_id_of(parent):
    """Descobre o espaço a partir da pasta ou página-mãe."""
    for kind in ('folders', 'pages'):
        st, d = call('GET', f'/wiki/api/v2/{kind}/{parent}')
        if st == 200:
            return d['spaceId'], d.get('title', '')
    sys.exit(f'mãe {parent} não encontrada como pasta nem como página')


def publicar(rel, page=None, parent=None, titulo=None, toc=False, nota=None, rotulos=None, mensagem=None,
             anexos=(), apply=False, allow_dirty=False, links=None, diagrams=None):
    """Publica `rel` (caminho relativo à raiz do repo) criando sob `parent` ou atualizando `page`.

    links/diagrams: repassados ao md2storage.convert (links entre documentos espelhados e diagramas
    Mermaid já renderizados e anexados). Devolve dict com pid, versão, título, url e ação; sem
    `apply`, só simula.
    """
    rotulos = (CFG.get('labels') or []) if rotulos is None else rotulos
    for x in anexos:
        if not os.path.isfile(x):
            sys.exit(f'anexo não encontrado: {x}')
    dirty = bool(git('status', '--porcelain', '--', rel))
    if dirty and not allow_dirty:
        sys.exit(f'{rel} tem mudança não commitada — commite antes ou use --allow-dirty')
    sha = git('rev-parse', '--short', 'HEAD')
    blob = git('hash-object', rel)[:10]
    hoje = datetime.date.today().isoformat()
    autor = myself()['displayName']
    md = open(os.path.join(ROOT, rel), encoding='utf-8').read()

    def log(line):
        line = f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{sha}\t{line}"
        print(line)
        if apply:
            with open(LOG, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    if toc:
        md, n = re.subn(r'^## Sumário\n.*?(?=^---\s*$|^## )', '@@TOC@@\n\n', md, count=1, flags=re.S | re.M)
        if not n:
            sys.exit('--toc pedido, mas o arquivo não tem seção "## Sumário"')

    body = ''
    if nota:
        body += ('<ac:structured-macro ac:name="info" ac:schema-version="1"><ac:rich-text-body>'
                 f'<p>{esc(nota)}</p></ac:rich-text-body></ac:structured-macro>')
    body += convert(md, src=rel, links=links, diagrams=diagrams).replace('<p>@@TOC@@</p>', TOC)
    body += (f'<hr /><p><em>Rastreio: fonte <code>{rel}</code> · commit {sha} (blob {blob})'
             f'{" (com alterações locais)" if dirty else ""} · sincronizado em {hoje} · '
             f'autor: {esc(autor)}.</em></p>')
    validate(body)
    mermaid = len(re.findall(r'^```mermaid', md, re.M))
    como_imagem = sum(1 for d in (diagrams or []) if d)

    if page:
        st, d = call('GET', f'/wiki/api/v2/pages/{page}', params={'body-format': 'storage'})
        if st != 200:
            sys.exit(f'GET da página {page} falhou (HTTP {st}): {d}')
        pid, titulo, old = page, d['title'], d['version']['number']
        if f'blob {blob}' in d['body']['storage']['value'] and not anexos:
            log(f'{rel}\t{pid}\tv{old}\tjá contém o blob {blob} — nada a fazer')
            return {'pid': pid, 'version': old, 'titulo': titulo, 'url': page_url(pid), 'acao': 'nada', 'blob': blob}
        acao = f'atualizar {pid} "{titulo}" v{old} → v{old + 1}'
    else:
        space, mae = space_id_of(parent)
        titulo = titulo or re.search(r'\A\s*#\s+(.+)$', open(os.path.join(ROOT, rel), encoding='utf-8').read(), re.M).group(1).strip()
        st, dup = call('GET', '/wiki/api/v2/pages', params={'space-id': space, 'title': titulo})
        if st == 200 and dup.get('results'):
            sys.exit(f'já existe página "{titulo}" no espaço (id {dup["results"][0]["id"]}) — use --page')
        acao = f'criar "{titulo}" sob "{mae}" ({parent})'

    if not apply:
        log(f'{rel}\tDRY-RUN\t{acao}\t{len(body)} bytes\t{mermaid} diagrama(s) Mermaid, {como_imagem} como imagem'
            f'\tanexos: {[os.path.basename(x) for x in anexos] or "—"}')
        return {'pid': page, 'version': None, 'titulo': titulo, 'url': page_url(page) if page else None,
                'acao': 'dry-run', 'blob': blob}

    # anexos antes do corpo: a página nova ainda não existe, então só depois do POST
    if page:
        for x in anexos:
            st_a, r_a = anexar(page, x)
            log(f'{rel}\t{page}\tanexo {os.path.basename(x)}\t'
                f'{"OK" if st_a in (200, 201) else f"ERRO HTTP {st_a}: {r_a}"}')
        st, r = call('PUT', f'/wiki/api/v2/pages/{pid}', {
            'id': pid, 'status': 'current', 'title': titulo,
            'body': {'representation': 'storage', 'value': body},
            'version': {'number': old + 1, 'message': mensagem or f'Sincronização de {rel} (commit {sha}).'}})
    else:
        st, r = call('POST', '/wiki/api/v2/pages', {
            'spaceId': space, 'parentId': parent, 'status': 'current', 'title': titulo,
            'body': {'representation': 'storage', 'value': body}})
        pid = r.get('id') if isinstance(r, dict) else None
        if pid:
            for x in anexos:
                st_a, r_a = anexar(pid, x)
                log(f'{rel}\t{pid}\tanexo {os.path.basename(x)}\t'
                    f'{"OK" if st_a in (200, 201) else f"ERRO HTTP {st_a}: {r_a}"}')
    if st not in (200, 201) or not pid:
        log(f'{rel}\tERRO HTTP {st}\t{str(r)[:300]}')
        sys.exit(1)

    st, lab = call('GET', f'/wiki/api/v2/pages/{pid}/labels')
    have = {x['name'] for x in lab.get('results', [])} if st == 200 else set()
    want = [x for x in rotulos if x and x not in have]
    if want:
        call('POST', f'/wiki/rest/api/content/{pid}/label', [{'prefix': 'global', 'name': x} for x in want])
    st, v = call('GET', f'/wiki/api/v2/pages/{pid}', params={'body-format': 'view'})
    erros = len(re.findall(r'(?i)unknown macro|error rendering|conf-macro-error', v['body']['view']['value']))
    versao = v['version']['number']
    log(f'{rel}\t{pid}\t{titulo}\tv{versao}\trótulos +{want or "—"}\t'
        f'{"OK" if not erros else f"ATENÇÃO: {erros} erro(s) de macro na renderização"}\t'
        f'{page_url(pid)}')
    return {'pid': pid, 'version': versao, 'titulo': titulo, 'url': page_url(pid), 'acao': 'publicado',
            'blob': blob, 'erros': erros}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('arquivo')
    alvo = ap.add_mutually_exclusive_group(required=True)
    alvo.add_argument('--parent', help='id da pasta ou página-mãe (cria página nova)')
    alvo.add_argument('--page', help='id da página a atualizar')
    ap.add_argument('--titulo', help='título da página nova (padrão: H1 do arquivo)')
    ap.add_argument('--toc', action='store_true', help='troca "## Sumário" pela macro de sumário')
    ap.add_argument('--nota', help='texto de um painel informativo no topo da página')
    ap.add_argument('--rotulos', default=','.join(CFG.get('labels') or []), help='rótulos separados por vírgula')
    ap.add_argument('--mensagem', help='mensagem da versão no Confluence')
    ap.add_argument('--anexo', action='append', default=[], help='arquivo a anexar (repetível)')
    ap.add_argument('--apply', action='store_true', help='publica de fato (sem isso é dry-run)')
    ap.add_argument('--allow-dirty', action='store_true', help='aceita arquivo com mudança não commitada')
    a = ap.parse_args()
    rel = os.path.relpath(os.path.abspath(a.arquivo), ROOT)
    r = publicar(rel, page=a.page, parent=a.parent, titulo=a.titulo, toc=a.toc, nota=a.nota,
                 rotulos=[x for x in a.rotulos.split(',') if x], mensagem=a.mensagem, anexos=a.anexo,
                 apply=a.apply, allow_dirty=a.allow_dirty)
    if r['acao'] == 'publicado':
        print('Lembrete: regenerar o índice — python3 scripts/sdd/gen-confluence-index.py'
              + (' e, se o arquivo for espelhado, rodar scripts/sdd/sync-mirror.py' if CFG.get('mirror', {}).get('enabled') else ''))


if __name__ == '__main__':
    main()
