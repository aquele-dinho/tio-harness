"""Converte uma spec SDD em Markdown (GFM) para o formato storage (XHTML) do Confluence.

- Blocos ``` viram macro `code` (Mermaid fica como código, como no M11 publicado).
- Tabelas com um único <tbody>; links relativos viram texto (não existem no Confluence), salvo os que
  apontam para documentos espelhados (`links`), que viram links para as páginas.
- Diagramas Mermaid renderizados (`diagrams`) viram imagem anexada; os demais ficam como código.
- `~~texto~~` vira tachado.
- Listas e citações no estilo GFM são ajustadas às regras do python-markdown.

Dependência: `markdown` (Python puro). Este ambiente não tem pip, então a wheel fixa abaixo é
baixada do PyPI na primeira execução, conferida pelo SHA-256 e extraída em scripts/sdd/.pylib/
(ignorado pelo Git).

Uso: python3 scripts/sdd/md2storage.py docs/sdd/<spec>.md > /tmp/x.xml
"""
import hashlib
import html
import importlib
import io
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

_PYLIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pylib')
_WHEEL = ('https://files.pythonhosted.org/packages/64/69/4a5af2bc115a9a33fefe51709749de8262be3f9ba063d1753a837cdbc49c/'
          'markdown-3.10.3-py3-none-any.whl')
_WHEEL_SHA256 = 'fa6c92a00a4a3c98b22728c64a935ae1928250ae65058a6ded814d2cc29a4cea'


def _load_markdown():
    sys.path.insert(0, _PYLIB)
    try:
        import markdown
        return markdown
    except ImportError:
        pass
    data = urllib.request.urlopen(_WHEEL, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != _WHEEL_SHA256:
        raise SystemExit('wheel do markdown com SHA-256 inesperado — abortado')
    zipfile.ZipFile(io.BytesIO(data)).extractall(_PYLIB)
    importlib.invalidate_caches()   # o diretório não existia na primeira tentativa de import
    import markdown
    return markdown


markdown = _load_markdown()

LIST_RE = re.compile(r'^(\s*)([-*+]|\d+[.)])\s+')


def _extract_code(md):
    blocks = []

    def repl(m):
        indent, lang, body = m.group(1), (m.group(2) or '').strip(), m.group(3)
        body = '\n'.join(l[len(indent):] if l.startswith(indent) else l for l in body.split('\n'))
        blocks.append((lang, body.rstrip('\n')))
        return f'{indent}CODEBLOCK{len(blocks) - 1}X'
    md = re.sub(r'^([ \t]*)```([^\n`]*)\n(.*?)\n[ \t]*```[ \t]*$', repl, md, flags=re.S | re.M)
    return md, blocks


def _quotes(md):
    """Aplica as regras também dentro de cada bloco citado (linhas iniciadas por '>')."""
    lines, out, i = md.split('\n'), [], 0
    while i < len(lines):
        m = re.match(r'^(\s*)>', lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        ind, block = m.group(1), []
        while i < len(lines) and re.match(r'^' + re.escape(ind) + r'>', lines[i]):
            block.append(re.sub(r'^' + re.escape(ind) + r'> ?', '', lines[i])); i += 1
        inner = _gfm_to_pymd(_quotes('\n'.join(block)))
        out += [ind + ('> ' + l if l.strip() else '>') for l in inner.split('\n')]
    return '\n'.join(out)


def _gfm_to_pymd(md):
    """Blank lines before lists/tables/quotes and 2→4-space nesting (python-markdown rules)."""
    out, prev, in_list = [], '', False
    for line in md.split('\n'):
        stripped = line.lstrip()
        is_list = bool(LIST_RE.match(line))
        is_table = stripped.startswith('|')
        is_quote = stripped.startswith('>')
        prev_s = prev.strip()
        if prev_s and not prev_s.startswith('|') and is_table:
            out.append('')
        elif prev_s and is_list and not in_list and not LIST_RE.match(prev):
            out.append('')
        elif prev_s and is_quote and not prev_s.startswith('>'):
            out.append('')
        elif prev_s.startswith('>') and stripped and not is_quote:
            out.append('')
        if is_list:
            in_list = True
        elif not stripped:
            in_list = False
        if in_list and line[:1] == ' ':
            n = len(line) - len(stripped)
            line = ' ' * (n * 2) + stripped
        out.append(line)
        prev = line
    return '\n'.join(out)


def _code_macro(lang, body):
    body = body.replace(']]>', ']]]]><![CDATA[>')
    lp = f'<ac:parameter ac:name="language">{lang}</ac:parameter>' if lang else ''
    return ('<ac:structured-macro ac:name="code" ac:schema-version="1">'
            '<ac:parameter ac:name="breakoutMode">wide</ac:parameter>' + lp +
            f'<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>')


def _image_macro(filename, width=None):
    w = f' ac:width="{width}"' if width else ''
    return f'<p><ac:image{w}><ri:attachment ri:filename="{html.escape(filename)}" /></ac:image></p>'


def _link(href, text, src, links):
    """Link relativo para documento espelhado vira link para a página; os demais viram texto."""
    if not links or not src:
        return text
    alvo, _, _ = href.partition('#')
    if not alvo:
        return text
    rel = os.path.normpath(os.path.join(os.path.dirname(src), html.unescape(alvo))).replace(os.sep, '/')
    url = links.get(rel)
    return f'<a href="{html.escape(url)}">{text}</a>' if url else text


def convert(md, src=None, links=None, diagrams=None):
    """src: caminho do .md relativo à raiz do repo; links: {caminho relativo à raiz: URL da página};
    diagrams: lista ordenada [(arquivo anexado, largura) | None] para os blocos Mermaid."""
    md = re.sub(r'\A\s*#\s[^\n]*\n', '', md)          # o título da página é o H1
    md, blocks = _extract_code(md)
    md = _gfm_to_pymd(_quotes(md))
    h = markdown.markdown(md, extensions=['tables', 'sane_lists'], output_format='xhtml')
    h = re.sub(r'~~(?=\S)(.+?)(?<=\S)~~', r'<s>\1</s>', h)
    n_mermaid = 0
    for i, (lang, body) in enumerate(blocks):
        mac = _code_macro(lang, body)
        if lang == 'mermaid':
            img = diagrams[n_mermaid] if diagrams and n_mermaid < len(diagrams) else None
            n_mermaid += 1
            if img:
                mac = _image_macro(*img)
        h, n = re.subn(rf'<p>\s*CODEBLOCK{i}X\s*</p>', lambda _: mac, h)
        if not n:
            h, n = re.subn(rf'CODEBLOCK{i}X', lambda _: mac, h)
        assert n == 1, f'bloco de código {i} não posicionado'
    # tabelas: um único tbody, sem atributos de alinhamento
    h = re.sub(r'</?thead>|</?tbody>', '', h)
    h = h.replace('<table>', '<table data-layout="default"><tbody>').replace('</table>', '</tbody></table>')
    h = re.sub(r'<(t[hd]) style="[^"]*">', r'<\1>', h)
    # links relativos/âncoras viram texto (não existem no Confluence)
    h = re.sub(r'<a href="(?!https?://)([^"]*)">(.*?)</a>', lambda m: _link(m.group(1), m.group(2), src, links), h,
               flags=re.S)
    h = re.sub(r'<br\s*>', '<br />', h)
    return h


def validate(storage):
    probe = re.sub(r'&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)\w+;', '', storage)
    ET.fromstring('<root xmlns:ac="urn:ac" xmlns:ri="urn:ri">' + probe + '</root>')


if __name__ == '__main__':
    s = convert(open(sys.argv[1]).read())
    validate(s)
    print(s)
