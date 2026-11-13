# Esquema do briefing

Referência de campos de `briefing.template.toml`: o que é, de onde costuma vir no documento de origem,
e onde aparece no harness instalado. **Obrigatórios** marcados com ★.

| Campo | Fonte típica | Usado em |
|---|---|---|
| ★ `project.name`, `project.slug`, `project.description` | título e objetivo do documento | todos os arquivos |
| `project.anchor_ticket`, `project.objectives` | termo de abertura, ticket | AGENTS.md §1, prompt `review` |
| ★ `project.output_language`, `project.chat_language` | convenção do time | AGENTS.md §1, skills |
| ★ `sources.primary` (`title`, `id`, `version_note`) | DRN / documento de regras de negócio | prompts SDD, skills, template de spec |
| `sources.documents[]` | referências citadas no documento | AGENTS.md §2, prompt `specify` |
| `sources.archive_dirs`, `sources.precedence` | política de documentação | AGENTS.md §2, §10 |
| `architecture.*` | documento/ADR de arquitetura | AGENTS.md §7, §10; `plan`; checklists |
| `architecture.directives[]` (`title`, `id`, `scope`, `topics`), `architecture.authority`, `architecture.proposals_dir` | diretrizes corporativas de arquitetura; quem decide; pasta das notas | AGENTS.md §7; `plan`; architecture-advisor |
| `stack.*` | README técnico, pom/package | implementation-loop, test-quality-loop, lane-executor |
| `stack.hexagonal.*` | padrão da casa (pack `java-spring-hexagonal`) | hexagonal-module, checklists |
| `repos[]` (`path`, `role`, `reference`) | onde o código vive; `reference = true` marca código só de consulta | AGENTS.md §7, implementation-loop (só os não-referência) |
| `domain.glossary[]` | glossário do documento | AGENTS.md §4 |
| `domain.state_machines[]` | máquinas de estado **explicitamente** listadas | AGENTS.md §5, test-scenarios |
| `domain.correlation_key` | chave de correlação/idempotência | glossário, checklists, testes |
| `domain.invariants[]` | regras que nunca podem ser violadas | §10, review, hexagonal-module, tl-review |
| `phasing.*` | faseamento/MVP | AGENTS.md §7, prompts, checklists |
| ★ `sdd.unit`, ★ `sdd.spec_dir` | como o time decompõe a spec | prompts SDD, template |
| `sdd.modules[]` | decomposição do escopo | AGENTS.md §6 |
| `sdd.pending[]` | pontos em aberto do documento | AGENTS.md §8 |
| `sdd.api_guidelines`, `security`, `observability`, `resilience`, `test_strategy`, `review_extra` | diretrizes de engenharia | `plan`, `review`, template, checklists |
| `vcs.*` | fluxo Git do time (`gitlab` → `glab`/MR; `github` → `gh`/PR) | source-intake, review-handoff, tl-review |
| `atlassian.*` | space/pasta/projeto Jira e custom fields | CONFLUENCE-SYNC, scripts REST, skills |
| `meetings.*` | pasta das transcrições, pasta das atas, pessoas a mencionar e onde publicar as atas (`publish`) | skill meeting-intake |
| `atlassian.mirror.*` | espelhamento dos documentos de autoria do projeto em páginas do Confluence (liga a regra, o mapa e o `sync-mirror.py`) | AGENTS.md §10, CONFLUENCE-SYNC, scripts REST |
| `rules.no_agent_attribution`, `rules.extra[]` | política do projeto/organização | AGENTS.md §10, skills |
| ★ `agents.targets` | ferramentas usadas pelo time | ponteiros e comandos por agente |
| ★ `packs.enabled` | escolha de capacidades | o que é instalado |

## Lacunas
- Obrigatório sem fonte → `"⚠️ A DEFINIR: <o que falta> — <quem responde>"`. O `validate` conta essas marcas
  e o `install` informa o total; elas aparecem no AGENTS.md gerado e viram pauta do time.
- Opcional sem fonte → vazio. A seção correspondente simplesmente não é gerada.

## Sintaxe dos templates (para quem mantém packs)
Arquivos `*.tmpl` são renderizados (o sufixo cai); os demais são copiados como estão. Caminhos também são renderizados.
- `@@caminho.do.campo@@` — valor (listas viram `` `a`, `b` ``); filtros `|json`, `|upper`, `|lower`.
- `@@if campo@@ … @@else@@ … @@endif@@` — também `not campo` e `campo == "valor"`.
- `@@for x in lista@@ … @@endfor@@` — com `loop.index`, `loop.first`, `loop.last`.
- Tag de bloco sozinha na linha some com a linha inteira.
- Derivados disponíveis: `agents.<alvo>` (bool), `has.<pack>` (bool, `-` vira `_`), `vcs.cli|mr|mr_long|view|diff|create|merge|note`,
  `tracker.name|key|key_example|status_in_progress`, `docs_platform`, `framework.version`.
- Condição por caminho no `pack.toml` (`[when]`): prefixo → expressão.
