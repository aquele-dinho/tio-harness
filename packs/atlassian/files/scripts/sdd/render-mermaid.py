#!/usr/bin/env python3
"""Renderiza os blocos Mermaid de um Markdown em PNG, na ordem em que aparecem.

Opcional: só funciona se o projeto configurar, em atlassian.mirror (briefing):
- mermaid_md2html: caminho de um conversor Node "md2html" (convert.mjs, pack md2html) com as dependências
  instaladas (npm ci ao lado), que grava <arquivo>.html com cada diagrama em SVG dentro de <figure class="diagram">;
- mermaid_browser: navegador Chromium/Edge para o screenshot em modo headless. Um caminho em /mnt/<x>/
  é tratado como executável do Windows (WSL), com arquivos temporários em C:\\Users\\Public.

Sem essa configuração, render() devolve None e os diagramas continuam como código no Confluence.

Uso: python3 scripts/sdd/render-mermaid.py <arquivo.md> <pasta-saída> [prefixo]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from atlassian_rest import CFG   # noqa: E402

TIMEOUT = 180


def _cfg():
    m = CFG.get('mirror') or {}
    return m.get('mermaid_md2html') or '', m.get('mermaid_browser') or ''


def disponivel():
    md2html, browser = _cfg()
    return bool(md2html and browser and os.path.isfile(md2html) and os.path.isfile(browser)
                and os.path.isdir(os.path.join(os.path.dirname(md2html), 'node_modules')))


def _svgs(md_path, md2html):
    tmp = tempfile.mkdtemp(prefix='tioh-mermaid-')
    try:
        copia = os.path.join(tmp, os.path.basename(md_path))
        shutil.copy(md_path, copia)
        subprocess.run(['node', md2html, copia], check=True, capture_output=True, timeout=TIMEOUT)
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
    windows = re.match(r'^/mnt/[a-z]/', browser)
    if windows:   # WSL chamando navegador do Windows: arquivos num caminho visível pelos dois lados
        nome = f'tioh-mermaid-{uuid.uuid4().hex[:8]}'
        base_wsl, base_win = '/mnt/c/Users/Public', 'C:/Users/Public'
        html_wsl, png_wsl = f'{base_wsl}/{nome}.html', f'{base_wsl}/{nome}.png'
        url, alvo, cwd = f'file:///{base_win}/{nome}.html', f'{base_win}/{nome}.png'.replace('/', '\\'), base_wsl
    else:
        tmp = tempfile.mkdtemp(prefix='tioh-mermaid-')
        html_wsl, png_wsl = os.path.join(tmp, 'd.html'), os.path.join(tmp, 'd.png')
        url, alvo, cwd = f'file://{html_wsl}', png_wsl, tmp
    open(html_wsl, 'w', encoding='utf-8').write(page)
    try:
        subprocess.run([browser, '--headless', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=2',
                        f'--window-size={w},{h}', f'--screenshot={alvo}', url],
                       cwd=cwd, capture_output=True, timeout=TIMEOUT)
        if not os.path.isfile(png_wsl):
            raise RuntimeError('o navegador não gerou o PNG')
        shutil.move(png_wsl, png)
    finally:
        for f in (html_wsl, png_wsl):
            if os.path.exists(f):
                os.remove(f)


def render(md_path, outdir, prefixo=None, nomes=None):
    """Devolve a lista de PNGs (na ordem dos blocos Mermaid) ou None se o renderizador não estiver
    configurado ou o arquivo não tiver diagramas. `nomes` fixa os nomes dos arquivos (anexos já existentes)."""
    md = open(md_path, encoding='utf-8').read()
    n = len(re.findall(r'^[ \t]*```mermaid', md, re.M))
    if not n or not disponivel():
        return None
    md2html, browser = _cfg()
    svgs = _svgs(md_path, md2html)
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
