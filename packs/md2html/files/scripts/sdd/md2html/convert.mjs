#!/usr/bin/env node
// Converte Markdown do repositório de especificação em HTML autocontido, gravado ao lado do .md:
//   - imagens locais embutidas em base64;
//   - diagramas Mermaid pré-renderizados em SVG (Edge headless do Windows, via interop do WSL);
//   - links para .md reescritos para .html (âncora preservada);
//   - âncoras de título no padrão do GitHub, para os sumários internos continuarem funcionando.
//
// Uso (a partir da raiz do repositório):
//   node scripts/sdd/md2html/convert.mjs                          # padrão: docs/sdd/arquitetura
//   node scripts/sdd/md2html/convert.mjs docs/sdd/arquitetura docs/sdd/analises/x.md
//
// Primeira vez: (cd scripts/sdd/md2html && npm ci)
// Variáveis opcionais: MSEDGE (caminho do msedge.exe), MD2HTML_TMP (pasta do Windows vista pelo WSL).
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { Marked } from 'marked';
import { gfmHeadingId } from 'marked-gfm-heading-id';

const require = createRequire(import.meta.url);
const HERE = path.dirname(new URL(import.meta.url).pathname);
const EDGE = process.env.MSEDGE || '/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const WIN_TMP = process.env.MD2HTML_TMP || '/mnt/c/Users/Public';
const MIME = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp' };
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---------- entradas
const args = process.argv.slice(2);
const inputs = (args.length ? args : ['docs/sdd/arquitetura']).flatMap((p) => {
  const abs = path.resolve(p);
  if (!fs.existsSync(abs)) throw new Error(`não encontrado: ${p}`);
  return fs.statSync(abs).isDirectory()
    ? fs.readdirSync(abs).filter((f) => f.endsWith('.md')).sort().map((f) => path.join(abs, f))
    : [abs];
});
const inRun = new Set(inputs);
const report = { images: 0, missingImages: [], mdLinks: 0, danglingLinks: [], diagrams: 0 };

// ---------- 1. coleta todos os diagramas (mesmo lexer da conversão) e renderiza de uma vez
const diagrams = [];
for (const file of inputs) {
  const toks = new Marked().lexer(fs.readFileSync(file, 'utf8'));
  new Marked().walkTokens(toks, (t) => { if (t.type === 'code' && t.lang === 'mermaid') diagrams.push(t.text); });
}
const svgs = renderMermaid(diagrams);

function renderMermaid(list) {
  if (!list.length) return [];
  if (!fs.existsSync(EDGE)) throw new Error(`Edge não encontrado em ${EDGE} (defina MSEDGE)`);
  const lib = fs.readFileSync(require.resolve('mermaid/dist/mermaid.min.js'), 'utf8');
  const body = list.map((d, i) => `<pre class="mermaid" id="mmd${i}">${esc(d)}</pre>`).join('\n');
  const page = `<!DOCTYPE html><html><head><meta charset="utf-8"><script>${lib}</script></head><body>${body}
<script>mermaid.initialize({startOnLoad:false,securityLevel:'loose',theme:'default',flowchart:{useMaxWidth:true},sequence:{useMaxWidth:true}});
mermaid.run({querySelector:'.mermaid'}).then(()=>document.body.setAttribute('data-done','1'),e=>document.body.setAttribute('data-error',String(e)));</script></body></html>`;
  const tmp = path.join(WIN_TMP, `md2html-${process.pid}.html`);
  fs.writeFileSync(tmp, page);
  const winPath = execFileSync('wslpath', ['-w', tmp]).toString().trim().replace(/\\/g, '/');
  let dom;
  try {
    dom = execFileSync(EDGE, ['--headless=new', '--disable-gpu', '--virtual-time-budget=60000', '--dump-dom', `file:///${winPath}`],
      { maxBuffer: 512 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'], timeout: 240000 }).toString();
  } finally { fs.rmSync(tmp, { force: true }); }
  if (!dom.includes('data-done="1"')) throw new Error('Mermaid não terminou: ' + (dom.match(/data-error="([^"]*)"/)?.[1] ?? 'sem sinal'));
  return list.map((_, i) => {
    const svg = dom.match(new RegExp(`<pre class="mermaid" id="mmd${i}"[^>]*>([\\s\\S]*?)</pre>`))?.[1].match(/<svg[\s\S]*<\/svg>/);
    if (!svg) throw new Error(`diagrama ${i} sem SVG`);
    if (/aria-roledescription="error"/.test(svg[0])) throw new Error(`diagrama ${i} com erro de sintaxe Mermaid`);
    return svg[0];
  });
}

// ---------- 2. converte cada arquivo
const CSS = fs.readFileSync(path.join(HERE, 'style.css'), 'utf8');
let dIdx = 0;
for (const file of inputs) {
  const dir = path.dirname(file);
  const rel = path.relative(process.cwd(), file);
  const md = fs.readFileSync(file, 'utf8');
  const marked = new Marked(gfmHeadingId());
  marked.use({
    gfm: true,
    renderer: {
      code({ text, lang }) {
        if (lang === 'mermaid') { report.diagrams++; return `<figure class="diagram">${svgs[dIdx++]}</figure>\n`; }
        return `<pre><code${lang ? ` class="language-${esc(lang)}"` : ''}>${esc(text)}</code></pre>\n`;
      },
    },
  });
  let html = marked.parse(md);

  html = html.replace(/(<img\b[^>]*?\bsrc=")([^"]+)(")/g, (all, a, src, b) => {
    if (/^(data:|https?:)/.test(src)) return all;
    const img = path.resolve(dir, decodeURIComponent(src.split('#')[0]));
    const mime = MIME[path.extname(img).toLowerCase()];
    if (!mime || !fs.existsSync(img)) { report.missingImages.push(`${rel}: ${src}`); return all; }
    report.images++;
    return `${a}data:${mime};base64,${fs.readFileSync(img).toString('base64')}${b}`;
  });

  html = html.replace(/(<a\b[^>]*?\bhref=")([^"]+)(")/g, (all, a, href, b) => {
    if (/^(https?:|mailto:|#)/.test(href)) return all;
    const [p, anchor] = href.split('#');
    if (!/\.md$/i.test(p)) return all;
    report.mdLinks++;
    const target = path.resolve(dir, decodeURIComponent(p));
    const targetHtml = target.replace(/\.md$/i, '.html');
    if (!inRun.has(target) && !fs.existsSync(targetHtml)) report.danglingLinks.push(`${rel} -> ${href}`);
    return `${a}${p.replace(/\.md$/i, '.html')}${anchor !== undefined ? '#' + anchor : ''}${b}`;
  });

  html = html.replace(/<table>/g, '<div class="table-wrap"><table>').replace(/<\/table>/g, '</table></div>');

  const title = (md.match(/^#\s+(.+)$/m)?.[1] ?? path.basename(file)).replace(/[*_`]/g, '');
  const out = `<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<style>
${CSS}
</style>
</head>
<body>
<main class="doc">
${html}
</main>
</body>
</html>
`;
  const dest = file.replace(/\.md$/, '.html');
  fs.writeFileSync(dest, out);
  console.log(`${rel} -> ${path.basename(dest)} (${(out.length / 1024).toFixed(0)} KB)`);
}
if (dIdx !== svgs.length) throw new Error(`diagramas usados ${dIdx} != renderizados ${svgs.length}`);
console.log(`\nimagens embutidas: ${report.images} · diagramas: ${report.diagrams} · links .md reescritos: ${report.mdLinks}`);
if (report.missingImages.length) console.log('⚠️ imagens não encontradas:\n  ' + report.missingImages.join('\n  '));
if (report.danglingLinks.length) console.log('⚠️ links para .md sem .html correspondente (converta também o destino):\n  ' + report.danglingLinks.join('\n  '));
