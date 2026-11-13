# AGENTS.md — Tio Harness

Repositório do framework que instala o harness agêntico SDD em projetos. Leia o
[README.md](README.md) primeiro.

## Idioma
Conversa em inglês ou português, conforme o usuário; artefatos (templates, docs, mensagens de commit) em **pt-BR**.

## Operar o framework (instalar/atualizar num projeto)
Use a skill `.agents/skills/framework-bootstrap` (Copilot: `/tioh-bootstrap`; Claude Code: `/tioh:bootstrap`).
Briefings de projetos atendidos ficam em `briefings/` e são **ignorados pelo Git** (só locais: carregam IDs e regras reais); o exemplo versionado é fictício, em `examples/`. Nunca grave segredos em briefing.

## Manter o framework
- **Genérico sempre:** nada de nome de projeto, ID de página, chave Jira ou custom field fixo dentro de `packs/` —
  tudo vem do briefing. Se uma skill precisa de um dado novo, crie o campo em `briefing/briefing.template.toml`,
  documente em `briefing/SCHEMA.md` e use um valor neutro quando ausente.
- **Templates:** só arquivos `*.tmpl` são renderizados. Não use `@@` literal em template. Scripts (`.py`, `.sh`) não
  são templates: leem a configuração de `.ai-framework/project.json` em tempo de execução.
- **Pack novo:** `packs/<nome>/pack.toml` (`name`, `description`, `requires`, `[when]` opcional) + `files/`.
- **Versão:** subir `VERSION` em `bin/tioh.py` a cada mudança que altere arquivos instalados — o `check` dos projetos
  passa a acusar a diferença.
- **Validação mínima antes de commitar:**
  ```bash
  python3 bin/tioh.py validate examples/gestor-pedidos.briefing.toml
  rm -rf /tmp/tioh-t && mkdir /tmp/tioh-t && python3 bin/tioh.py install examples/gestor-pedidos.briefing.toml /tmp/tioh-t
  grep -rn '@@' /tmp/tioh-t --exclude-dir=scripts ; python3 bin/tioh.py check /tmp/tioh-t
  ```
- Não registrar uso de agentes em commits, comentários ou documentos; sem `Co-Authored-By`.
- Sem remoto por padrão; commits só quando solicitado.
