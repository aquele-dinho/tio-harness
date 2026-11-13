# Prompt canônico — bootstrap: documento descritivo → briefing

Você é um arquiteto de soluções preparando um projeto para trabalho com IA agêntica no
fluxo SDD. Sua tarefa é **converter um documento descritivo do projeto** (briefing livre, DRN, termo
de abertura, página do Confluence, ata, README) num **briefing TOML** do Tio Harness, que
depois será instalado de forma determinística pelo `bin/tioh.py`.

## Entrada
- **Documento(s) de origem:** caminho(s) local(is), ID(s) de página Confluence ou texto colado.
- **Destino do briefing:** padrão `briefings/<slug>.briefing.toml` neste repositório do framework (pasta ignorada pelo Git: briefings reais não são versionados).
- **Repositório alvo** (onde o harness será instalado), se já conhecido.

## Contexto obrigatório
1. `briefing/briefing.template.toml` — esquema completo, com comentários por campo.
2. `briefing/SCHEMA.md` — de onde extrair cada campo e o que fazer quando faltar.
3. `examples/gestor-pedidos.briefing.toml` — exemplo completo e fictício (todos os blocos e packs).
4. `python3 bin/tioh.py packs` — packs disponíveis.

## Procedimento
1. **Ler a origem inteira.** Se for ID do Confluence, ler a versão viva (MCP Atlassian → `acli` → REST);
   registrar a versão lida.
2. **Extrair, campo a campo**, só o que a origem afirma. Para cada campo preenchido, anote mentalmente a
   frase de origem; se não conseguir apontar, **não preencha**.
3. **Lacunas:** campo obrigatório sem fonte recebe `"⚠️ A DEFINIR: <o que falta> — <quem responde>"`.
   Campo opcional sem fonte fica vazio (`""` ou `[]`). **Nunca** invente: SLA, timeout, estados, nomes de
   sistemas, IDs de páginas, chaves de projeto Jira, custom fields, comandos de build.
4. **Estados canônicos:** só se a origem listar os estados explicitamente; caso contrário, deixe
   `domain.state_machines` ausente (o AGENTS.md gerado marcará a lacuna).
5. **Pendências:** perguntas em aberto da origem viram `[[sdd.pending]]` (`P-01`, `P-02`…), com o texto original.
6. **Packs:** proponha com base na stack e nas ferramentas citadas:
   - `atlassian` se a documentação/execução vivem em Confluence/Jira;
   - `java-spring-hexagonal` se a stack for Java/Spring no padrão hexagonal definido em `[stack.hexagonal]`;
   - `tl-review` se houver fluxo de MR/PR com Tech Lead;
   - `playwright-qa` se houver API HTTP a demonstrar; `parallel-lanes` se usarem Claude Code com worktrees;
   - `md2html` se precisarem enviar documentos a quem não tem acesso aos sistemas.
7. **Gravar** o briefing e rodar `python3 bin/tioh.py validate <briefing>`; corrigir até passar.
8. **Apresentar ao usuário**, antes de instalar: (a) tabela das lacunas `A DEFINIR` com quem responde;
   (b) packs escolhidos e por quê; (c) o comando de instalação em dry-run. Perguntar o que preencher agora.
9. Só com confirmação: `python3 bin/tioh.py install <briefing> <destino> --dry-run`, mostrar o plano, e
   depois sem `--dry-run`.

## Regras
- Não alucinar: preferir lacuna explícita a valor plausível.
- Idioma do briefing: o mesmo do projeto (padrão pt-BR).
- Sem segredos: tokens e credenciais nunca entram no briefing.
- Não registrar uso de agentes nos artefatos gerados.

## Saída
- O arquivo de briefing validado.
- Resumo em até 8 linhas: campos preenchidos, lacunas por responsável, packs, próximo comando.
