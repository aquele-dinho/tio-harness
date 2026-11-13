#!/usr/bin/env python3
"""Mantém os documentos de autoria do projeto espelhados em páginas do Confluence (atlassian.mirror).

Cada arquivo em `include` (menos `exclude`) tem uma página espelho registrada no mapa
(`atlassian.mirror.map`, padrão docs/sdd/confluence-mirror.json) com a versão e o blob do último sync.
Assim dá para saber, sem ambiguidade, quem mudou desde então: o arquivo local, a página ou os dois.

Comandos:
    sync-mirror.py                                   status (somente leitura)
    sync-mirror.py push [arquivo ...] [--apply]      publica os arquivos "local à frente" (todos, se nenhum for dado)
    sync-mirror.py push --create ARQ [--parent ID] [--apply]   cria a página espelho de um arquivo novo
    sync-mirror.py adopt ARQ --page ID               registra uma página já existente como espelho de ARQ
    sync-mirror.py diff ARQ                          texto do arquivo local × texto da página atual

Gate (AGENTS.md §10): atualizar página espelho já registrada a partir do arquivo local tem autorização
permanente. Criar página (--create) e sobrescrever página editada remotamente (--remoto-incorporado)
exigem confirmação humana explícita. Sem --apply é sempre dry-run.

Edição remota: o status mostra "remoto alterado". Rode `diff`, incorpore a edição no arquivo local,
commite e publique com `push ARQ --remoto-incorporado --apply` (com confirmação humana).
"""
import argparse
import datetime
import difflib
import fnmatch
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from atlassian_rest import CFG, call, page_url   # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
M = CFG.get('mirror') or {}
MAP = os.path.join(ROOT, M.get('map') or 'docs/sdd/confluence-mirror.json')


def _modulo(nome, arquivo):
    spec = importlib.util.spec_from_file_location(nome, os.path.join(HERE, arquivo))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(*args):
    return subprocess.run(['git', '-C', ROOT, *args], capture_output=True, text=True, check=True).stdout.strip()


def carregar():
    if os.path.isfile(MAP):
        return json.load(open(MAP, encoding='utf-8'))
    return {'_sobre': 'Mapa arquivo local → página espelho no Confluence (scripts/sdd/sync-mirror.py). '
                      'Gerado e atualizado pelo script; não editar à mão.', 'paginas': {}}


def salvar(mapa):
    mapa['paginas'] = dict(sorted(mapa['paginas'].items()))
    with open(MAP, 'w', encoding='utf-8') as f:
        json.dump(mapa, f, ensure_ascii=False, indent=2)
        f.write('\n')


def no_escopo():
    inc = M.get('include') or ['docs/**/*.md']
    exc = (M.get('exclude') or []) + (M.get('exclude_extra') or []) + [os.path.relpath(MAP, ROOT)]
    vistos = set()
    for padrao in inc:
        for p in sorted(__import__('glob').glob(os.path.join(ROOT, padrao), recursive=True)):
            rel = os.path.relpath(p, ROOT).replace(os.sep, '/')
            if os.path.isfile(p) and not any(fnmatch.fnmatch(rel, e) for e in exc):
                vistos.add(rel)
    return sorted(vistos)


def blob(rel):
    return git('hash-object', rel)[:10]


def sujo(rel):
    return bool(git('status', '--porcelain', '--', rel))


def remoto(pid):
    st, d = call('GET', f'/wiki/api/v2/pages/{pid}')
    if st == 404:
        return None
    if st != 200:
        sys.exit(f'GET da página {pid} falhou (HTTP {st}): {d}')
    return d['version']['number']


def situacao(rel, entrada):
    if not os.path.isfile(os.path.join(ROOT, rel)):
        return 'arquivo removido', None
    if not entrada:
        return 'sem página', None
    v = remoto(entrada['page'])
    if v is None:
        return 'página ausente', None
    local = blob(rel) != entrada.get('blob')
    rem = v != entrada.get('version')
    estado = {(False, False): 'em dia', (True, False): 'local à frente',
              (False, True): 'remoto alterado', (True, True): 'conflito'}[(local, rem)]
    return estado, v


def cmd_status(a):
    mapa = carregar()
    arquivos = sorted(set(no_escopo()) | set(mapa['paginas']))
    linhas = []
    for rel in arquivos:
        e = mapa['paginas'].get(rel)
        estado, v = situacao(rel, e)
        nota = 'não commitado' if os.path.isfile(os.path.join(ROOT, rel)) and sujo(rel) else ''
        ver = f"v{e['version']}→v{v}" if e and v and v != e.get('version') else (f"v{v}" if v else '—')
        linhas.append((estado, rel, e['page'] if e else '—', ver, nota))
    ordem = ['conflito', 'remoto alterado', 'página ausente', 'arquivo removido', 'local à frente', 'sem página', 'em dia']
    linhas.sort(key=lambda x: (ordem.index(x[0]), x[1]))
    w = max([len(x[1]) for x in linhas] + [7])
    for estado, rel, pid, ver, nota in linhas:
        print(f'{estado:<16} {rel:<{w}} {pid:<11} {ver:<9} {nota}')
    resumo = {}
    for x in linhas:
        resumo[x[0]] = resumo.get(x[0], 0) + 1
    print('\n' + ' · '.join(f'{k}: {v}' for k, v in resumo.items()))
    if resumo.get('remoto alterado') or resumo.get('conflito'):
        print('Há edição remota: rode `sync-mirror.py diff <arquivo>`, incorpore no arquivo local e só então publique.')


def _links(mapa):
    return {rel: page_url(e['page']) for rel, e in mapa['paginas'].items()}


def _publicar(pp, rel, mapa, page=None, parent=None, apply=False, mensagem=None):
    rm = _modulo('render_mermaid', 'render-mermaid.py')
    e = mapa['paginas'].get(rel) or {}
    anexos, diagrams = [], None
    tmp = tempfile.mkdtemp(prefix='tioh-mirror-')
    pngs = rm.render(os.path.join(ROOT, rel), tmp, nomes=e.get('diagramas'))
    if pngs:
        anexos = pngs
        diagrams = [(os.path.basename(p), rm.largura(p)) for p in pngs]
    rotulos = list(dict.fromkeys((CFG.get('labels') or []) + (M.get('labels') or [])))
    return pp.publicar(rel, page=page, parent=parent, rotulos=rotulos, anexos=anexos, apply=apply,
                       links=_links(mapa), diagrams=diagrams,
                       mensagem=mensagem or f'Espelho de {rel} (commit {git("rev-parse", "--short", "HEAD")}).'), pngs


def _registrar(mapa, rel, r, pngs):
    e = mapa['paginas'].setdefault(rel, {})
    e.update({'page': str(r['pid']), 'version': r['version'], 'blob': r['blob'],
              'synced': datetime.datetime.now().isoformat(timespec='seconds')})
    if pngs:
        e['diagramas'] = [os.path.basename(p) for p in pngs]


def cmd_push(a):
    pp = _modulo('publish_page', 'publish-page.py')
    mapa = carregar()
    if a.create:
        rel = os.path.relpath(os.path.abspath(a.create), ROOT).replace(os.sep, '/')
        if rel in mapa['paginas']:
            sys.exit(f'{rel} já tem página espelho ({mapa["paginas"][rel]["page"]}) — use push sem --create')
        parent = a.parent or M.get('parent')
        if not parent:
            sys.exit('sem página-mãe: passe --parent ou defina atlassian.mirror.parent no briefing')
        r, pngs = _publicar(pp, rel, mapa, parent=parent, apply=a.apply)
        if a.apply:
            _registrar(mapa, rel, r, pngs)
            salvar(mapa)
            print(f'registrado no mapa: {rel} → {r["pid"]} v{r["version"]} — commite {os.path.relpath(MAP, ROOT)}')
        return
    alvos = [os.path.relpath(os.path.abspath(x), ROOT).replace(os.sep, '/') for x in a.arquivos] or list(mapa['paginas'])
    feitos = 0
    for rel in alvos:
        e = mapa['paginas'].get(rel)
        if not e:
            print(f'{rel}: sem página espelho — use --create (exige confirmação humana)')
            continue
        estado, _ = situacao(rel, e)
        if estado == 'em dia':
            if a.arquivos:
                print(f'{rel}: em dia')
            continue
        if estado in ('página ausente', 'arquivo removido'):
            print(f'{rel}: {estado} — resolva à mão')
            continue
        if estado in ('remoto alterado', 'conflito') and not a.remoto_incorporado:
            print(f'{rel}: {estado} — não publicado. Rode diff, incorpore a edição remota no arquivo e use '
                  '--remoto-incorporado (exige confirmação humana).')
            continue
        if sujo(rel):
            print(f'{rel}: mudança não commitada — commite antes de publicar')
            continue
        r, pngs = _publicar(pp, rel, mapa, page=e['page'], apply=a.apply)
        if a.apply and r['acao'] in ('publicado', 'nada'):
            _registrar(mapa, rel, r, pngs)
            feitos += 1
    if a.apply and feitos:
        salvar(mapa)
        print(f'mapa atualizado ({feitos} página(s)) — commite {os.path.relpath(MAP, ROOT)} e regenere o índice')


def cmd_adopt(a):
    rel = os.path.relpath(os.path.abspath(a.arquivo), ROOT).replace(os.sep, '/')
    st, d = call('GET', f'/wiki/api/v2/pages/{a.page}', params={'body-format': 'storage'})
    if st != 200:
        sys.exit(f'página {a.page} não encontrada (HTTP {st})')
    m = re.search(r'blob ([0-9a-f]{7,10})', d['body']['storage']['value'])
    mapa = carregar()
    mapa['paginas'][rel] = {'page': str(a.page), 'version': d['version']['number'],
                            'blob': m.group(1) if m else '',
                            'synced': datetime.datetime.now().isoformat(timespec='seconds')}
    if a.diagramas:
        mapa['paginas'][rel]['diagramas'] = [x for x in a.diagramas.split(',') if x]
    salvar(mapa)
    estado, _ = situacao(rel, mapa['paginas'][rel])
    print(f'{rel} → {a.page} v{d["version"]["number"]} (blob no rodapé: {m.group(1) if m else "—"}) — {estado}')


def _texto(storage):
    s = re.sub(r'<(br|/p|/tr|/li|/h\d)[^>]*>', '\n', storage)
    s = re.sub(r'<(td|th)[^>]*>', ' | ', s)
    s = html.unescape(re.sub(r'<[^>]+>', '', s))
    return [l.strip() for l in s.split('\n') if l.strip()]


def cmd_diff(a):
    rel = os.path.relpath(os.path.abspath(a.arquivo), ROOT).replace(os.sep, '/')
    e = carregar()['paginas'].get(rel)
    if not e:
        sys.exit(f'{rel} não tem página espelho')
    conv = _modulo('md2storage', 'md2storage.py')
    local = _texto(conv.convert(open(os.path.join(ROOT, rel), encoding='utf-8').read(), src=rel))
    st, d = call('GET', f'/wiki/api/v2/pages/{e["page"]}', params={'body-format': 'storage'})
    pagina = _texto(re.sub(r'<hr\s*/?>\s*<p><em>Rastreio:.*$', '', d['body']['storage']['value'], flags=re.S))
    diff = list(difflib.unified_diff(local, pagina, f'local {rel}', f'página {e["page"]} v{d["version"]["number"]}',
                                     n=1, lineterm=''))
    print('\n'.join(diff) if diff else 'sem diferença de texto (pode haver diferença só de formatação)')


def main():
    if not M.get('enabled'):
        sys.exit('espelhamento desligado: defina atlassian.mirror.enabled = true no briefing e reinstale o harness')
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('push')
    p.add_argument('arquivos', nargs='*')
    p.add_argument('--create', metavar='ARQ')
    p.add_argument('--parent')
    p.add_argument('--remoto-incorporado', action='store_true',
                   help='o arquivo local já incorpora a edição remota; sobrescreve a página (confirmação humana)')
    p.add_argument('--apply', action='store_true')
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser('adopt')
    p.add_argument('arquivo')
    p.add_argument('--page', required=True)
    p.add_argument('--diagramas', help='nomes dos anexos dos diagramas, na ordem (para manter os existentes)')
    p.set_defaults(fn=cmd_adopt)
    p = sub.add_parser('diff')
    p.add_argument('arquivo')
    p.set_defaults(fn=cmd_diff)
    a = ap.parse_args()
    (getattr(a, 'fn', None) or cmd_status)(a)


if __name__ == '__main__':
    main()
