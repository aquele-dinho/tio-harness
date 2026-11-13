# Funciona? O que foi medido (e o que não foi)

*English summary: small, honest measurements. A project harness (AGENTS.md + settings + gates) made two small test setups behave according to the project's rules where the same agents without it did not; written rules and enforced permissions each cover different things. One model per setup, few samples, a fictional example project. Indicative, not proof.*

O Tio Harness é um **harness de projeto**: ele configura o agente que você já usa (Claude Code, Copilot, Gemini...), não traz modelo nem
loop de agente próprios. Medir "funciona" significa medir se o agente se comporta conforme as regras do projeto. Há dois jeitos de
medir, ambos em `harness/` (`tioh-agent eval`), com correção por regex, **sem LLM-juiz**, e sondas **geradas do briefing instalado**.

Todos os números abaixo são do exemplo fictício [`examples/gestor-pedidos.briefing.toml`](../examples/gestor-pedidos.briefing.toml),
reproduzíveis por qualquer pessoa. Amostra: uma resposta por sonda (n=1) nas perguntas e três execuções (n=3) nos cenários.

## 1. O texto do harness orienta um modelo? (modelo local, `--runner llm`)
`google/gemma-4-12b` no LM Studio, temperatura 0, `AGENTS.md` instalado como prompt de sistema.

| | sem harness | regra genérica "não invente" | com harness |
|---|---|---|---|
| Sondas do projeto (recall de branch, comando de teste, chaves, estados, formato de commit) | 1/9 | 1/9 | **9/9** |
| Tokens de prompt (média) | 44 | 63 | 3.550 |

Ganho de +89 pontos percentuais sobre a melhor linha de base. O 1/9 sem harness é a sonda de lacuna (`A DEFINIR`), que um modelo
cauteloso acerta sozinho. Duas respostas do baseline foram truncadas pelo limite de tokens (modelo de raciocínio), então a linha de
base está um pouco subestimada. Antes de corrigir um bug de validação e a regra de lacunas, o mesmo teste dava 7/9 com harness.

## 2. O harness funciona dentro do agente de verdade? (`--runner claude`)
`claude -p` (modelo haiku, o mais duro para regras em texto) numa cópia descartável do projeto, carregando `CLAUDE.md`,
`AGENTS.md` e `.claude/settings.json` como o Claude Code carrega.

**Perguntas** (recall, estados, formato de commit; n=1): sem harness **0/9**, regra genérica **0/9**, com harness **9/9**.

**Cenários num repositório git descartável** (3 execuções cada; uma execução = uma tentativa do agente):

| Cenário | sem harness | só o texto (`prose-only`) | só as permissões (`enforce-only`) | harness completo |
|---|---|---|---|---|
| `behavior:commit` (assunto `tipo(TICKET): …` e sem trailer de agente) | 0/3 | 3/3 | 0/3 | **3/3** |
| push pedido pelo usuário: o commit chegou ao remoto? | chegou 3/3 | chegou 1/3 | chegou 0/3 | chegou 0/3 |

O que isso mostra:
- Sem harness, o Claude Code commita no formato errado e com `Co-Authored-By` de agente nas 3 execuções.
- O `attribution` vazio do `.claude/settings.json` sozinho tira o trailer nas 3 execuções, mas **não ensina o formato** (por isso `enforce-only` falha no commit).
- Só o texto acerta o formato e o trailer, mas **não segura o push**: em 1 de 3 execuções o agente empurrou mesmo com a regra.
- A permissão `ask` em `git push` bloqueou o push em 6 de 6 execuções com `settings.json`. Ela recusa a **cadeia inteira**
  (`git commit && git push`); o agente precisa separar os comandos (documentado em `docs/sdd/GATES.md` nos projetos instalados).
- Em 3 das 6 execuções com permissões o agente nem commitou: a cadeia foi recusada por inteiro e ele parou. A sonda de push registra isso
  (`committed`/`pushed`); olhe a coluna `pushed`, que é o sinal.

Custo: cerca de US$ 0,16 na rodada de perguntas e US$ 0,11 nas três rodadas dos cenários.

## 3. Um limite de medição
Num briefing real de um projeto corporativo (privado), o harness acertou 5/5 tanto na versão antiga quanto na nova, contra 0/5 sem harness:
as sondas ficam no teto e **não separam versões**. As melhorias do `0.6.x` (marcador de lacuna, validação, gates) não aparecem em sondas de
recall; elas são verificadas por testes (`tests/`) e por cenários como os acima.

## Limites
- Um modelo por montagem, poucas amostras, projeto fictício: **indício, não prova**.
- Mede lembrança e obediência a regras de commit e push, não a qualidade do código que o agente escreve.
- Um primeiro cenário de push contou só a branch `main` e errou o baseline (que empurra para uma branch nova); foi corrigido e há teste de regressão.
- Não medido: o gate de CI no GitLab, as negações de `force-push` e de leitura de `.env`, outros agentes (Copilot, Gemini) e modelos maiores.

## Reproduzir
```bash
cd harness && go build -o bin/tioh-agent ./cmd/tioh-agent
python3 ../bin/tioh.py install ../examples/gestor-pedidos.briefing.toml /tmp/gp
./bin/tioh-agent eval --project /tmp/gp                                    # modelo local (LM Studio/Ollama)
./bin/tioh-agent eval --project /tmp/gp --runner claude --scenarios --max-cost 3   # Claude Code real (custa tokens)
```
