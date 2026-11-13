#!/usr/bin/env python3
"""Renderiza os blocos Mermaid de um Markdown em PNG, na ordem em que aparecem.

Opcional: só funciona se o projeto configurar, em atlassian.mirror (briefing):
- mermaid_md2html: caminho de um conversor Node "md2html" (convert.mjs, pack md2html) com as dependências
  instaladas (npm ci ao lado), que grava <arquivo>.html com cada diagrama em SVG dentro de <figure class="diagram">;
- mermaid_browser (opcional): navegador Chromium para o screenshot em modo headless. Vazio = autodetecção
  (Chrome, Edge, Chromium ou Brave) em macOS, Linux, Windows e WSL. Também vale a env MD2HTML_BROWSER.
  Um caminho configurado que não existe nesta máquina gera um aviso e cai na autodetecção.

No WSL, um navegador do Windows (/mnt/<x>/....exe) é chamado via interop: os arquivos temporários ficam em
C:\\Users\\Public (visível pelos dois lados) e os caminhos são convertidos com `wslpath`.

Sem a configuração do conversor, render() devolve None e os diagramas continuam como código no Confluence.

Uso: python3 scripts/sdd/render-mermaid.py <arquivo.md> <pasta-saída> [prefixo]
"""
import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from atlassian_rest import CFG   # noqa: E402

TIMEOUT = 180
WSL_TMP = '/mnt/c/Users/Public'   # pasta do Windows vista pelo WSL (existe em toda instalação do Windows)

_PATH_NAMES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge',
               'microsoft-edge-stable', 'brave-browser', 'chrome', 'msedge', 'brave')
_MAC_APPS = ('Google Chrome', 'Microsoft Edge', 'Chromium', 'Brave Browser')
_WIN_EXES = ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe',
             'BraveSoftware/Brave-Browser/Application/brave.exe')


def _candidatos(plataforma=None, env=None, home=None):
    """Caminhos prováveis de um navegador Chromium, em ordem de preferência (sem conferir existência)."""
    plataforma = plataforma or sys.platform
    env = os.environ if env is None else env
    home = home or str(Path.home())
    for nome in _PATH_NAMES:                                   # PATH primeiro: vale em qualquer SO
        achado = shutil.which(nome, path=env.get('PATH'))
        if achado:
            yield achado
    if plataforma == 'darwin':
        for base in ('/Applications', f'{home}/Applications'):
            for app in _MAC_APPS:
                yield f'{base}/{app}.app/Contents/MacOS/{app}'
    if plataforma == 'win32':
        for var in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            if env.get(var):
                for exe in _WIN_EXES:
                    yield str(PureWindowsPath(env[var], *exe.split('/')))
    for base in ('/mnt/c/Program Files', '/mnt/c/Program Files (x86)'):   # WSL: navegador do Windows
        for exe in _WIN_EXES:
            yield f'{base}/{exe}'


@functools.lru_cache(maxsize=None)   # disponivel() e render() chamam; o aviso sai uma vez só
def achar_navegador(configurado=''):
    """Navegador a usar: configurado (se existir) > env MD2HTML_BROWSER > autodetecção. '' se não houver."""
    explicito = os.path.expanduser(configurado or '') or os.environ.get('MD2HTML_BROWSER', '')
    if explicito:
        if os.path.isfile(explicito):
            return explicito
        print(f'aviso: navegador configurado não existe nesta máquina ({explicito}); tentando autodetecção',
              file=sys.stderr)
    for c in _candidatos():
        if os.path.isfile(c):
            return c
    return ''


def _windows_via_wsl(browser):
    """True quando estamos no Linux/WSL e o navegador é um executável do Windows (precisa de interop)."""
    return sys.platform.startswith('linux') and (re.match(r'^/mnt/[a-z]/', browser) is not None
                                                 or browser.lower().endswith('.exe'))


def _cfg():
    m = CFG.get('mirror') or {}
    return os.path.expanduser(m.get('mermaid_md2html') or ''), m.get('mermaid_browser') or ''


def disponivel():
    md2html, browser = _cfg()
    return bool(md2html and os.path.isfile(md2html) and shutil.which('node')
                and os.path.isdir(os.path.join(os.path.dirname(md2html), 'node_modules'))
                and achar_navegador(browser))


def _svgs(md_path, md2html, browser):
    tmp = tempfile.mkdtemp(prefix='tioh-mermaid-')
    try:
        copia = os.path.join(tmp, os.path.basename(md_path))
        shutil.copy(md_path, copia)
        env = dict(os.environ, MD2HTML_BROWSER=browser)        # os dois estágios usam o mesmo navegador
        r = subprocess.run([shutil.which('node') or 'node', md2html, copia], capture_output=True, timeout=TIMEOUT,
                           env=env)
        if r.returncode:
            raise RuntimeError('md2html falhou: ' + r.stderr.decode('utf-8', 'replace').strip()[-400:])
        html = open(copia[:-3] + '.html', encoding='utf-8').read()
        return re.findall(r'<figure class="diagram">(.*?)</figure>', html, re.S)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _shot(svg, png, browser):
    vb = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+)"', svg)
    w, h = int(float(vb.group(3))) + 20, int(float(vb.group(4))) + 20
    svg = re.sub(r'style="max-width:[^"]*"', '', svg, count=1)
    svg = re.sub(r'width="100%"', f'width="{w - 20}"', svg, count=1)
    page = f'<!doctype html><meta charset="utf-8"><body style="margin:10px;background:#fff">{svg}</body>'
    via_wsl = _windows_via_wsl(browser)
    # No WSL+navegador do Windows a pasta temporária precisa ser visível pelos dois lados.
    tmp = tempfile.mkdtemp(prefix='tioh-mermaid-', dir=WSL_TMP if via_wsl else None)
    html, saida = os.path.join(tmp, 'd.html'), os.path.join(tmp, 'd.png')
    try:
        with open(html, 'w', encoding='utf-8') as f:
            f.write(page)
        if via_wsl:   # caminhos na visão do Windows
            raiz = subprocess.check_output(['wslpath', '-w', tmp]).decode().strip()
            win = PureWindowsPath(raiz)
            url, alvo, perfil = (win / 'd.html').as_uri(), str(win / 'd.png'), str(win / 'perfil')
        else:
            url, alvo, perfil = Path(html).as_uri(), saida, os.path.join(tmp, 'perfil')
        args = [browser, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=2',
                '--no-first-run', f'--user-data-dir={perfil}', f'--window-size={w},{h}', f'--screenshot={alvo}']
        if hasattr(os, 'geteuid') and os.geteuid() == 0:        # Chromium não abre o sandbox como root (containers)
            args.append('--no-sandbox')
        r = subprocess.run(args + [url], cwd=tmp, capture_output=True, timeout=TIMEOUT)
        if not os.path.isfile(saida):
            detalhe = r.stderr.decode('utf-8', 'replace').strip()[-300:]
            raise RuntimeError('o navegador não gerou o PNG' + (f': {detalhe}' if detalhe else ''))
        shutil.move(saida, png)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render(md_path, outdir, prefixo=None, nomes=None):
    """Devolve a lista de PNGs (na ordem dos blocos Mermaid) ou None se o renderizador não estiver
    configurado ou o arquivo não tiver diagramas. `nomes` fixa os nomes dos arquivos (anexos já existentes)."""
    md = open(md_path, encoding='utf-8').read()
    n = len(re.findall(r'^[ \t]*```mermaid', md, re.M))
    if not n or not disponivel():
        return None
    md2html, configurado = _cfg()
    browser = achar_navegador(configurado)
    svgs = _svgs(md_path, md2html, browser)
    if len(svgs) != n:
        raise RuntimeError(f'{md_path}: {n} bloco(s) Mermaid, {len(svgs)} renderizado(s)')
    os.makedirs(outdir, exist_ok=True)
    prefixo = prefixo or os.path.splitext(os.path.basename(md_path))[0]
    saida = []
    for i, svg in enumerate(svgs, 1):
        nome = nomes[i - 1] if nomes and i <= len(nomes) else f'{prefixo}-{i}.png'
        png = os.path.join(outdir, nome)
        _shot(svg, png, browser)
        saida.append(png)
    return saida


def largura(png, maximo=1200):
    """Largura de exibição: metade dos pixels (screenshot em escala 2), limitada a `maximo`."""
    with open(png, 'rb') as f:
        cab = f.read(24)
    return min(int.from_bytes(cab[16:20], 'big') // 2, maximo)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    r = render(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    if r is None:
        sys.exit('nada renderizado: sem blocos Mermaid ou renderizador não configurado (atlassian.mirror)')
    for p in r:
        print(p, largura(p))
