#!/usr/bin/env python3
"""Aplica um changeset no Jira (projeto de .ai-framework/project.json) a partir de um JSON — idempotente, com dry-run e log por item.

Sem --apply é dry-run: relê o estado atual e mostra o que faria. Escrever no Jira exige gate
humano (AGENTS.md §10). Formato do JSON: scripts/sdd/examples/changeset-exemplo.json.

Blocos, nesta ordem (--blocos escolhe um subconjunto):
  criar         cria itens (com SP); grava a chave criada de volta no JSON ("key")
  mover         troca o pai ("de" → "para"); se o pai atual não for "de", registra CONFLITO
  renomear      troca o resumo ("de" → "para"); só grava se o resumo atual for "de" (senão CONFLITO)
  atualizar_sp  grava Story Points só se o valor atual for o "de" (senão registra CONFLITO)
  rotular       adiciona rótulos
  desrotular    remove rótulos
  vincular      cria link "de" → "para" do "tipo" (Blocks: "de" bloqueia "para"; Relates; Duplicate)
  definir_descricao  troca a descrição por "paragrafos" (+ "referencia", URL em cartão); só grava
                se a atual começar por "espera_inicio" (senão CONFLITO)
  copiar_descricao  copia a descrição de "de" para "para"; destino já preenchido e diferente é
                CONFLITO, salvo com "sobrescrever": true
  cancelar     comentário + link opcional + SP zerado opcional + transição para atlassian.cancel_status
  transicionar  comentário opcional + transição para o status "para"
  sprint        põe "issues" na sprint "sprint" (id do board); "sprint": null devolve ao backlog
  atribuir      atribui "issues" ao "responsavel" ("me" = quem roda o script, ou accountId)
  comentar      comentário avulso (texto ou lista de parágrafos), sem mudar o item

Regras aprendidas na prática com o Jira Cloud:
- Nome de status comparado sem caixa (alguns projetos exibem "EM DESENVOLVIMENTO").
- O id da transição muda por workflow: resolve-se pelo nome do status de destino, item a item.
- A tela de cancelamento pode exigir Motivo, Justificativa e Solicitante (+ confirmação); só os
  campos presentes na tela são enviados (atlassian.cancel_fields no briefing).
- Link: {"inwardIssue": A, "outwardIssue": B} com tipo Duplicate = "A duplicates B".
- Referência a item criado no mesmo changeset: "@ref:<ref>" em "pai", "link.para", "vincular" e "sprint".
- Sprint: campo atlassian.sprint_field; a API Agile só move itens de sprint aberta/futura (item Pronto
  numa sprint ativa fica onde está).
- Comentários não mencionam ferramenta nem autoria automatizada (AGENTS.md §10).

Uso:
    python3 scripts/sdd/jira-changeset.py docs/sdd/analises/changeset-X.json                 # dry-run
    python3 scripts/sdd/jira-changeset.py docs/sdd/analises/changeset-X.json --blocos criar --apply
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atlassian_rest import CFG, SP_FIELD, adf, call, myself, search   # noqa: E402

BLOCOS = ['criar', 'mover', 'renomear', 'atualizar_sp', 'rotular', 'desrotular', 'vincular', 'definir_descricao',
          'copiar_descricao', 'cancelar', 'transicionar', 'sprint', 'atribuir', 'comentar']
SPRINT_FIELD = CFG.get('sprint_field') or ''
CANCEL_STATUS = CFG.get('cancel_status') or 'Cancelado'
# Campos da tela de cancelamento (confirmacao, motivo, justificativa, solicitante), preenchidos só se estiverem na tela.
CAMPOS_CANCELAMENTO = CFG.get('cancel_fields') or {}


def sem_local_id(n):
    """ADF sem os localId, que o Jira gera por item, e sem "attrs" que fique vazio (comparação e cópia)."""
    if isinstance(n, dict):
        out = {k: ({a: b for a, b in v.items() if a != 'localId'} if k == 'attrs' else sem_local_id(v))
               for k, v in n.items()}
        if out.get('attrs') == {}:
            del out['attrs']
        return out
    return [sem_local_id(c) for c in n] if isinstance(n, list) else n


def texto(n):
    """Texto corrido de um nó ADF."""
    if isinstance(n, dict):
        return n.get('text', '') + ''.join(texto(c) for c in n.get('content', []))
    return ''


class Changeset:
    def __init__(self, path, apply):
        self.path, self.apply = path, apply
        self.cs = json.load(open(path))
        self.logf = os.path.splitext(path)[0] + '.log'
        self.projeto = self.cs.get('projeto') or CFG.get('jira_project') or sys.exit('defina "projeto" no changeset ou atlassian.jira_project no briefing')
        self.bloco = '-'
        self._me = None

    # ------------------------------------------------------------------ util
    def log(self, key, acao, resultado):
        line = (f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{self.bloco}\t{key}\t{acao}\t"
                f"{resultado if self.apply or resultado.startswith(('já', 'CONFLITO', 'ERRO')) else 'DRY-RUN: ' + resultado}")
        print(line)
        if self.apply:
            with open(self.logf, 'a') as f:
                f.write(line + '\n')

    def save(self):
        if self.apply:
            with open(self.path, 'w') as f:
                json.dump(self.cs, f, ensure_ascii=False, indent=1)

    def me(self):
        if self._me is None:
            d = myself()
            self._me = f"{d['displayName']} <{d.get('emailAddress', '')}>"
        return self._me

    def ref(self, value):
        if isinstance(value, str) and value.startswith('@ref:'):
            name = value[5:]
            for x in self.cs.get('criar', []):
                if x.get('ref') == name:
                    return x.get('key') or ('<nova:' + name + '>')
            raise SystemExit(f'referência {value} não existe em "criar"')
        return value

    def fields(self, key, f='summary,status,labels,issuelinks,' + SP_FIELD):
        st, d = call('GET', f'/rest/api/3/issue/{key}', params={'fields': f})
        if st != 200:
            raise SystemExit(f'GET {key}: HTTP {st} {d}')
        return d['fields']

    def must(self, st, d, ok, what):
        if st not in ok:
            raise SystemExit(f'{what}: HTTP {st} {d}')

    def comment(self, key, text):
        """Idempotente: não repete um comentário cujo início (60 caracteres) já exista.
        `text` pode ser uma string ou uma lista de parágrafos."""
        paras = text if isinstance(text, list) else [text]
        st, d = call('GET', f'/rest/api/3/issue/{key}/comment', params={'maxResults': 100})
        if any(paras[0][:60] in json.dumps(c.get('body', {}), ensure_ascii=False) for c in d.get('comments', [])):
            return 'já existe'
        if self.apply:
            self.must(*call('POST', f'/rest/api/3/issue/{key}/comment', {'body': adf(*paras)}), (200, 201), f'comentário {key}')
        return 'OK'

    def set_sp(self, key, value):
        if self.apply:
            self.must(*call('PUT', f'/rest/api/3/issue/{key}', {'fields': {SP_FIELD: value}}), (204,), f'SP {key}')

    def link(self, src, dst, tipo, f):
        for l in f.get('issuelinks', []):
            if l['type']['name'] == tipo and dst in (l.get('outwardIssue', {}).get('key'), l.get('inwardIssue', {}).get('key')):
                return 'já existe'
        if self.apply:
            self.must(*call('POST', '/rest/api/3/issueLink', {
                'type': {'name': tipo}, 'inwardIssue': {'key': src}, 'outwardIssue': {'key': dst}}), (201,), f'link {src}→{dst}')
        return 'OK'

    def transition(self, key, destino, justificativa=None):
        st, d = call('GET', f'/rest/api/3/issue/{key}/transitions', params={'expand': 'transitions.fields'})
        ts = [t for t in d.get('transitions', []) if t['to']['name'].casefold() == destino.casefold()]
        if not ts:
            return None
        t, tela, campos = ts[0], ts[0].get('fields', {}), {}
        valores = {'confirmacao': {'value': 'Sim'},
                   'motivo': {'value': self.cs.get('cancelamento', {}).get('motivo', 'Criação Incorreta')},
                   'justificativa': adf(justificativa) if justificativa else None,
                   'solicitante': None}
        for nome, fid in CAMPOS_CANCELAMENTO.items():
            if fid in tela:
                campos[fid] = self.me() if nome == 'solicitante' else valores.get(nome)
        campos = {k: v for k, v in campos.items() if v is not None}
        if self.apply:
            body = {'transition': {'id': t['id']}}
            if campos:
                body['fields'] = campos
            self.must(*call('POST', f'/rest/api/3/issue/{key}/transitions', body), (204,), f'transição {key}')
        return t['id'] + (f' +{len(campos)} campos' if campos else '')

    # ------------------------------------------------------------------ blocos
    def criar(self):
        for x in self.cs.get('criar', []):
            pai = self.ref(x.get('pai'))
            if x.get('key'):
                self.log(x['key'], f"criar {x.get('ref', '')}", 'já existe')
                continue
            if pai and not pai.startswith('<nova:'):
                hit = [i['key'] for i in search(f'project = {self.projeto} AND parent = {pai}', ['summary'])
                       if i['fields']['summary'].strip() == x['resumo'].strip()]
                if hit:
                    x['key'] = hit[0]
                    self.save()
                    self.log(hit[0], f"criar {x.get('ref', '')}", 'já existe (mesmo resumo sob o pai)')
                    continue
            campos = {'project': {'key': self.projeto}, 'issuetype': {'name': x['tipo']}, 'summary': x['resumo'][:250],
                      'labels': x.get('rotulos', self.cs.get('rotulos_padrao', []))}
            if pai:
                campos['parent'] = {'key': pai}
            if x.get('sp') is not None:
                campos[SP_FIELD] = x['sp']
            if x.get('descricao'):
                campos['description'] = adf(*x['descricao'])
            if self.apply:
                st, d = call('POST', '/rest/api/3/issue', {'fields': campos})
                self.must(st, d, (200, 201), f"criar {x.get('ref', x['resumo'][:40])}")
                x['key'] = d['key']
                self.save()
            self.log(x.get('key', '-'), f"criar {x.get('ref', '')} ({x['tipo']} sob {pai}, {x.get('sp')} SP)", 'OK')

    def mover(self):
        for x in self.cs.get('mover', []):
            f = self.fields(x['key'], 'parent')
            atual = (f.get('parent') or {}).get('key')
            acao = f"pai {x['de']} → {x['para']}"
            if atual == x['para']:
                self.log(x['key'], acao, 'já aplicado')
            elif atual != x['de']:
                self.log(x['key'], acao, f'CONFLITO: pai atual é {atual} — ignorado')
            else:
                if self.apply:
                    self.must(*call('PUT', f"/rest/api/3/issue/{x['key']}", {'fields': {'parent': {'key': x['para']}}}),
                              (204,), f"mover {x['key']}")
                self.log(x['key'], acao, 'OK')

    def renomear(self):
        for x in self.cs.get('renomear', []):
            atual = self.fields(x['key'], 'summary')['summary'].strip()
            acao = f"resumo → {x['para'][:60]}"
            if atual == x['para'].strip():
                self.log(x['key'], acao, 'já aplicado')
            elif atual != x['de'].strip():
                self.log(x['key'], acao, f'CONFLITO: resumo atual é "{atual[:60]}"')
            else:
                if self.apply:
                    self.must(*call('PUT', f"/rest/api/3/issue/{x['key']}", {'fields': {'summary': x['para'][:250]}}),
                              (204,), f"resumo {x['key']}")
                self.log(x['key'], acao, 'OK')

    def atualizar_sp(self):
        itens = self.cs.get('atualizar_sp', [])
        snap = {}
        keys = [x['key'] for x in itens]
        for i in range(0, len(keys), 80):
            for it in search(f'key in ({",".join(keys[i:i + 80])})', ['status', SP_FIELD]):
                snap[it['key']] = it['fields']
        for x in itens:
            f = snap.get(x['key'])
            if f is None:
                self.log(x['key'], 'SP', 'ERRO: item não encontrado')
                continue
            cur = f.get(SP_FIELD)
            cur = int(cur) if isinstance(cur, float) and cur.is_integer() else cur
            if f['status']['name'].casefold() == CANCEL_STATUS.casefold():
                self.log(x['key'], 'SP', 'CONFLITO: item cancelado — ignorado')
            elif cur == x['para']:
                self.log(x['key'], f"SP {x['para']}", 'já aplicado')
            elif cur != x.get('de'):
                self.log(x['key'], f"SP {x.get('de')}→{x['para']}", f'CONFLITO: Jira tem {cur} — ignorado')
            else:
                self.set_sp(x['key'], x['para'])
                self.log(x['key'], f"SP {x.get('de')}→{x['para']}", 'OK')

    def rotular(self):
        for x in self.cs.get('rotular', []):
            if x['rotulo'] in self.fields(x['key'], 'labels').get('labels', []):
                self.log(x['key'], f"rótulo {x['rotulo']}", 'já aplicado')
                continue
            if self.apply:
                self.must(*call('PUT', f"/rest/api/3/issue/{x['key']}", {'update': {'labels': [{'add': x['rotulo']}]}}),
                          (204,), f"rótulo {x['key']}")
            self.log(x['key'], f"rótulo {x['rotulo']}", 'OK')

    def desrotular(self):
        for x in self.cs.get('desrotular', []):
            if x['rotulo'] not in self.fields(x['key'], 'labels').get('labels', []):
                self.log(x['key'], f"remover rótulo {x['rotulo']}", 'já ausente')
                continue
            if self.apply:
                self.must(*call('PUT', f"/rest/api/3/issue/{x['key']}", {'update': {'labels': [{'remove': x['rotulo']}]}}),
                          (204,), f"remover rótulo {x['key']}")
            self.log(x['key'], f"remover rótulo {x['rotulo']}", 'OK')

    def vincular(self):
        for x in self.cs.get('vincular', []):
            de, para = self.ref(x['de']), self.ref(x['para'])
            acao = f"link {x['tipo']} {de} → {para}"
            if de.startswith('<nova:') or para.startswith('<nova:'):
                self.log(de, acao, 'OK (após criar)')
                continue
            self.log(de, acao, self.link(de, para, x['tipo'], self.fields(de, 'issuelinks')))

    def definir_descricao(self):
        for x in self.cs.get('definir_descricao', []):
            novo = adf(*x['paragrafos'])
            if x.get('referencia'):
                novo['content'].append({'type': 'paragraph', 'content': [
                    {'type': 'text', 'text': 'Referência do módulo: '},
                    {'type': 'inlineCard', 'attrs': {'url': x['referencia']}}]})
            atual = self.fields(x['key'], 'description').get('description')
            if atual and sem_local_id(atual) == novo:
                self.log(x['key'], 'definir descrição', 'já aplicado')
                continue
            if not texto(atual).startswith(x.get('espera_inicio', '')):
                self.log(x['key'], 'definir descrição', f"CONFLITO: descrição atual não começa por \"{x['espera_inicio'][:50]}\" — ignorado")
                continue
            if self.apply:
                self.must(*call('PUT', f"/rest/api/3/issue/{x['key']}", {'fields': {'description': novo}}),
                          (204,), f"descrição {x['key']}")
            self.log(x['key'], 'definir descrição', 'OK')

    def copiar_descricao(self):
        for x in self.cs.get('copiar_descricao', []):
            src = self.fields(x['de'], 'description').get('description')
            dst = self.fields(x['para'], 'description').get('description')
            acao = f"descrição {x['de']} → {x['para']}"
            if not src:
                self.log(x['para'], acao, 'ERRO: origem sem descrição')
                continue
            src = sem_local_id(src)
            if dst and sem_local_id(dst) == src:
                self.log(x['para'], acao, 'já aplicado')
                continue
            if dst and not x.get('sobrescrever'):
                self.log(x['para'], acao, 'CONFLITO: destino já tem outra descrição — ignorado')
                continue
            if self.apply:
                self.must(*call('PUT', f"/rest/api/3/issue/{x['para']}", {'fields': {'description': src}}),
                          (204,), f"descrição {x['para']}")
            self.log(x['para'], acao, 'OK')

    def cancelar(self):
        for x in self.cs.get('cancelar', []):
            key = x['key']
            f = self.fields(key)
            if f['status']['name'].casefold() == CANCEL_STATUS.casefold():
                self.log(key, 'cancelar', 'já cancelado')
                continue
            if x.get('zerar_sp') and f.get(SP_FIELD):
                self.set_sp(key, 0)
                self.log(key, f'SP {f.get(SP_FIELD)}→0', 'OK')
            if x.get('link'):
                dst = self.ref(x['link']['para'])
                self.log(key, f"link {x['link']['tipo']} → {dst}", self.link(key, dst, x['link']['tipo'], f))
            if x.get('comentario'):
                self.log(key, 'comentário', self.comment(key, x['comentario']))
            tid = self.transition(key, CANCEL_STATUS, x.get('comentario'))
            self.log(key, f'cancelar (transição {tid})', 'OK' if tid else f'ERRO: sem transição para {CANCEL_STATUS}')

    def transicionar(self):
        for x in self.cs.get('transicionar', []):
            f = self.fields(x['key'])
            if f['status']['name'].casefold() == x['para'].casefold():
                self.log(x['key'], f"status → {x['para']}", 'já aplicado')
                continue
            if x.get('comentario'):
                self.log(x['key'], 'comentário', self.comment(x['key'], x['comentario']))
            tid = self.transition(x['key'], x['para'], x.get('comentario'))
            self.log(x['key'], f"status {f['status']['name']} → {x['para']} (transição {tid})",
                     'OK' if tid else 'ERRO: sem transição disponível')

    def sprint(self):
        for x in self.cs.get('sprint', []):
            alvo = x.get('sprint')
            for key in [self.ref(k) for k in x['issues']]:
                if key.startswith('<nova:'):
                    self.log(key, f'sprint → {alvo}', 'OK (após criar)')
                    continue
                f = self.fields(key, f'status,{SPRINT_FIELD}')
                abertas = [s['id'] for s in (f.get(SPRINT_FIELD) or []) if s.get('state') in ('active', 'future')]
                acao = f"sprint {abertas or 'backlog'} → {alvo or 'backlog'}"
                if (alvo is None and not abertas) or (alvo is not None and alvo in abertas):
                    self.log(key, acao, 'já aplicado')
                    continue
                if self.apply:
                    if alvo is None:
                        self.must(*call('POST', '/rest/agile/1.0/backlog/issue', {'issues': [key]}), (204,), f'backlog {key}')
                    else:
                        self.must(*call('POST', f'/rest/agile/1.0/sprint/{alvo}/issue', {'issues': [key]}),
                                  (204,), f'sprint {key}')
                self.log(key, acao, 'OK')

    def atribuir(self):
        for x in self.cs.get('atribuir', []):
            alvo = myself()['accountId'] if x['responsavel'] == 'me' else x['responsavel']
            for key in [self.ref(k) for k in x['issues']]:
                if key.startswith('<nova:'):
                    self.log(key, f"responsável → {x['responsavel']}", 'OK (após criar)')
                    continue
                atual = self.fields(key, 'assignee').get('assignee') or {}
                acao = f"responsável {atual.get('displayName', 'nenhum')} → {x['responsavel']}"
                if atual.get('accountId') == alvo:
                    self.log(key, acao, 'já aplicado')
                    continue
                if self.apply:
                    self.must(*call('PUT', f'/rest/api/3/issue/{key}/assignee', {'accountId': alvo}), (204,),
                              f'responsável {key}')
                self.log(key, acao, 'OK')

    def comentar(self):
        for x in self.cs.get('comentar', []):
            self.fields(x['key'], 'summary')
            n = len(x['comentario']) if isinstance(x['comentario'], list) else 1
            self.log(x['key'], f'comentário ({n} parágrafo(s))', self.comment(x['key'], x['comentario']))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('changeset', help='caminho do JSON')
    ap.add_argument('--blocos', default=','.join(BLOCOS), help='subconjunto, ex.: criar,atualizar_sp')
    ap.add_argument('--apply', action='store_true', help='escreve no Jira (sem isso é dry-run)')
    a = ap.parse_args()
    cs = Changeset(a.changeset, a.apply)
    for b in [b for b in BLOCOS if b in a.blocos.split(',')]:
        cs.bloco = b
        getattr(cs, b)()
    cs.save()


if __name__ == '__main__':
    main()
