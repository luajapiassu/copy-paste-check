# Roadmap

Regra geral: **tudo que dá para provar com regra é determinístico; o LLM só interpreta evidência já calculada** e nunca decide sozinho. Manter simples: pouca dependência, hook rápido, pouco CPU.

## Feito

- [x] Índice incremental em SQLite com `exact` e `struct` (sempre ligados)
- [x] `formula` (sympy) e `near` (MinHash) **opcionais**, desligados por padrão (`formula = true` / `near = true` no `.cpcheck.toml`)
- [x] CLI `scan` / `check` / `diff` (com `--markdown`)
- [x] Hook do Claude Code: bloqueia `exact`/`struct` (PreToolUse), avisa `formula`/`near` (PostToolUse)
- [x] Skill para agentes
- [x] Configuração por repositório (`.cpcheck.toml`)

## Próximo

- [ ] Etapa de PR: `cpcheck diff` no pre-push / pr-review. Sobra de `exact`/`struct` é reportada direto (a prova é o hash).
- [ ] Subagent Haiku (somente leitura) só nos candidatos `near`/`formula`: recebe o par de funções + métricas e responde uma linha ("duplicata real, reusar X" / "coincidência" / "intencional").

## Pontas soltas

- [ ] Rodar no corpus do wright-apps e listar as duplicatas `exact`/`struct` existentes (medir ruído e acerto reais)
- [ ] Construir o passo Haiku (candidatos `near`/`formula`) e a integração no pr-review
- [ ] Testar o `diff` contra `origin/main` num repo de teste com commits
- [ ] TS/TSX: hoje só Python (no wright-apps o `apps/web` fica de fora)
- [ ] Decidir distribuição: biblioteca (pip por URL git) vs plugin do Claude Code; repo público ou privado
- [ ] Decidir adoção em outro repo: hook no `settings.json`, passo no pre-push, ADR

## Fora de escopo (por ora)

GitHub Action, índice compartilhado entre repositórios, duplicidade de dados no Postgres, fórmulas fora de funções, detecção de algoritmos diferentes com o mesmo resultado. Reabrir só se o custo de manutenção compensar.
