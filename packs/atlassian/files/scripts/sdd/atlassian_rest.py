"""Cliente REST mínimo do Atlassian Cloud (Jira + Confluence) para os scripts de scripts/sdd/.

Configuração do projeto: .ai-framework/project.json (gerado pelo Tio Harness a partir do
briefing), seção "atlassian". Variáveis de ambiente sobrepõem: ATLASSIAN_SITE.

Credencial: ~/.config/atlassian-mcp/rest.authorization.header (header HTTP pronto) ou a env
ATLASSIAN_REST_AUTHORIZATION. Nunca é impressa.

Uso típico (o `acli` não lê nem edita custom fields):
    from atlassian_rest import call, search, myself, CFG
    status, body = call('GET', f"/rest/api/3/issue/{CFG['jira_project']}-1", params={'fields': 'status'})
"""
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PROJECT_FILE = ROOT / '.ai-framework' / 'project.json'
HDR_FILE = pathlib.Path.home() / '.config/atlassian-mcp/rest.authorization.header'


def _load_cfg():
    data = json.loads(PROJECT_FILE.read_text(encoding='utf-8')) if PROJECT_FILE.is_file() else {}
    cfg = dict(data.get('atlassian', {}))
    cfg['_project'] = data.get('project', {})
    cfg['_sdd'] = data.get('sdd', {})
    if os.environ.get('ATLASSIAN_SITE'):
        cfg['site'] = os.environ['ATLASSIAN_SITE']
    return cfg


CFG = _load_cfg()
BASE = (CFG.get('site') or '').rstrip('/')
SP_FIELD = CFG.get('story_points_field') or ''
SPEC_DIR = CFG['_sdd'].get('spec_dir', 'docs/sdd/modulos')
LOG_DIR = ROOT / 'docs' / 'sdd' / 'analises'


def _require_base():
    if not BASE:
        sys.exit(f'site Atlassian não configurado: defina atlassian.site no briefing ({PROJECT_FILE}) '
                 'ou exporte ATLASSIAN_SITE')


def page_url(pid):
    space = CFG.get('space')
    return f'{BASE}/wiki/spaces/{space}/pages/{pid}' if space else f'{BASE}/wiki/pages/viewpage.action?pageId={pid}'


def _auth():
    # O arquivo vem primeiro: é o que costuma ser reescrito numa rotação de token.
    if HDR_FILE.is_file():
        return re.sub(r'^[Aa]uthorization:\s*', '', HDR_FILE.read_text().strip())
    env = os.environ.get('ATLASSIAN_REST_AUTHORIZATION', '').strip()
    if env:
        return env
    sys.exit('sem credencial: grave ~/.config/atlassian-mcp/rest.authorization.header '
             'ou exporte ATLASSIAN_REST_AUTHORIZATION')


def call(method, path, body=None, params=None, timeout=60):
    """Retorna (status HTTP, JSON ou texto do erro). Não levanta exceção em HTTP 4xx/5xx."""
    _require_base()
    url = (path if path.startswith('http') else BASE + path) + \
        ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': _auth(), 'Accept': 'application/json', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:1000]


def search(jql, fields, page_size=100):
    """JQL paginada (POST /rest/api/3/search/jql, via nextPageToken)."""
    out, token = [], None
    while True:
        body = {'jql': jql, 'fields': fields, 'maxResults': page_size}
        if token:
            body['nextPageToken'] = token
        st, d = call('POST', '/rest/api/3/search/jql', body)
        if st != 200:
            raise SystemExit(f'JQL falhou (HTTP {st}): {d}')
        out += d.get('issues', [])
        token = d.get('nextPageToken')
        if not token or d.get('isLast'):
            return out


def myself():
    st, d = call('GET', '/rest/api/3/myself')
    if st != 200:
        raise SystemExit(f'/myself falhou (HTTP {st}): {d}')
    return d


def adf(*paragraphs):
    """Documento ADF simples (um parágrafo por string; None/'' são ignorados)."""
    return {'type': 'doc', 'version': 1,
            'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': p}]}
                        for p in paragraphs if p]}
