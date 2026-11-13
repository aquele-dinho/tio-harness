---
name: framework-bootstrap
description: >-
  Use esta skill para preparar um novo projeto para IA agêntica com o Tio Harness:
  converte um briefing livre ou documento descritivo (DRN, termo de abertura, página Confluence) num briefing TOML validado,
  escolhe os packs e instala no repositório alvo AGENTS.md, prompts SDD, skills, comandos por agente e scripts.
  Acione quando o usuário pedir para instalar, inicializar, extrair ou atualizar o framework/harness num projeto.
---

# Skill: Framework Bootstrap

## Quando usar
- Projeto novo: "instala o framework no projeto X a partir deste documento".
- Projeto existente: "atualiza o harness do projeto X" (reinstalação) ou "o harness está em dia?" (`check`).

## Processo
1. **Projeto novo, a partir de documento:** seguir `bootstrap/briefing-from-document.md` até o briefing passar no `validate`.
2. **Projeto novo, a partir de entrevista** (sem documento): `python3 bin/tioh.py init briefings/<slug>.briefing.toml`
   e perguntar ao usuário os campos obrigatórios e os que ele souber; o resto fica `⚠️ A DEFINIR`.
3. **Plano:** `python3 bin/tioh.py install <briefing> <destino> --dry-run`. Mostrar o resumo (CREATE/UPDATE/CONFLICT/OBSOLETO).
4. **Instalar** só com confirmação explícita. Conflitos viram `<arquivo>.tioh-new` — nunca sobrescrever com `--force`
   sem o usuário pedir; ajudar a fazer o merge manual do `.tioh-new`.
5. **Pós-instalação:** recomendar versionar `.ai-framework/` junto com o projeto; rodar `scripts/sdd/install-agent-skills.sh .`
   se o Claude Code for usado na raiz; listar as lacunas `A DEFINIR` restantes como primeira pauta do time.
6. **Atualização:** o briefing vigente fica em `<destino>/.ai-framework/briefing.toml`. Editar lá (ou no repositório do
   framework) e reinstalar. `python3 bin/tioh.py check <destino>` sai 1 se houver drift.

## Regras
- Não alucinar valores no briefing (ver o prompt de bootstrap).
- Escrita só no destino informado; nunca no próprio repositório do framework.
- Sem segredos no briefing nem na saída.
