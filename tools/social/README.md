# Rascunhos de posts a cada versão (`tools/social`)

Quando uma versão nova chega à `main` (ou `master`), o workflow [`social-drafts`](../../.github/workflows/social-drafts.yml)
gera rascunhos para **LinkedIn**, **X** e uma entrada de **build log** para o site, e os abre como uma issue com o
rótulo `social-draft`. **Nada é publicado automaticamente**: você revisa, ajusta, publica à mão e fecha a issue.

Isto é ferramenta pessoal do repositório, não um pack: não é instalado nos projetos e não altera `VERSION`.

## Quando gera
| Situação no push | Resultado |
| --- | --- |
| `VERSION` em `bin/tioh.py` mudou | gera |
| algum commit com `[post]` na mensagem | gera |
| algum commit com `[skip post]` ou `[no post]` | não gera (vence os outros) |
| nada disso | não gera |
| `workflow_dispatch` (aba Actions) | gera, se `force` estiver marcado |

Assim, um post por release, não por push.

## Privacidade
- Posts **nunca** citam cliente, empregador, pessoa ou ID interno: o prompt ([voice.md](voice.md)) exige descrição
  genérica ("a team I work with").
- Secret opcional `SOCIAL_DENYLIST` (termos separados por vírgula ou linha): cada termo vira `[redacted]` **antes**
  de o texto sair do runner, e o texto gerado é conferido de novo. Se algum termo aparecer, o rascunho é bloqueado.
  Guarde a lista como secret, nunca no repositório.
- E-mails nos commits também são removidos.

## Texto com IA ou template
- **Sem configuração:** sai um esqueleto com o que mudou e lacunas entre colchetes para completar.
- **Com o secret `ANTHROPIC_API_KEY`:** o texto é escrito pelo Claude (modelo em `SOCIAL_MODEL`, padrão
  `claude-opus-5-5`, esforço médio, saída em JSON validado). Custa alguns centavos por release e os commits (já sem
  os termos da denylist) vão para a API. Falha do modelo não derruba nada: cai no template.

Variáveis do repositório (Settings → Secrets and variables → Actions → *Variables*), todas opcionais:
`SOCIAL_LANG` (`en` padrão, ou `pt`), `SOCIAL_MODEL`.

## Rodar localmente
```bash
python3 tools/social/draft_posts.py --before <sha-antes> --after HEAD          # imprime o markdown
python3 tools/social/draft_posts.py --after HEAD --force --no-llm --out /tmp/post.md
```

## Usar em outro repositório
Copie `tools/social/` e o workflow; ajuste `--project`, `SOCIAL_VERSION_FILE` e `SOCIAL_VERSION_REGEX` (ex.:
`pom.xml` e `<version>([^<]+)</version>`).
