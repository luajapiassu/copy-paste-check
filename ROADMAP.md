# Roadmap

Regra geral: **tudo que dá para provar com regra é determinístico; o LLM só interpreta evidência já calculada** e nunca decide sozinho. Manter simples: pouca dependência, hook rápido, pouco CPU.

## Feito

- [x] Índice incremental em SQLite com `exact` e `struct` (sempre ligados)
- [x] `formula` (sympy) e `near` (MinHash) **opcionais**, desligados por padrão (`formula = true` / `near = true` no `.cpcheck.toml`)
- [x] CLI `scan` / `check` / `diff` (com `--markdown`)
- [x] Hook do Claude Code: bloqueia `exact`/`struct` (PreToolUse), avisa `formula`/`near` (PostToolUse)
- [x] Skill para agentes
- [x] Configuração por repositório (`.cpcheck.toml`)
- [x] `near` com nota ponderada por raridade (IDF): MinHash+LSH só acha candidatos; boilerplate compartilhado (CRUD, try/commit/return) pesa pouco. Padrão `near_threshold = 0.75`
- [x] `formula_max_funcs` (padrão 5): fórmula presente em funções demais é idioma do projeto, não cópia; função aninhada não é duplicata do pai
- [x] Worktree nova herda o índice de outra worktree do mesmo repo (backup do SQLite): 1,1 s em vez de ~10 s no corpus do wright-apps
- [x] Rodado no corpus do wright-apps (2026-11-11): `exact`/`struct` achou `_append_last_date` (4 arquivos de mktmonitor), `_parse_br_number` (~8 templates de extrato), `load` dos pipelines ANBIMA, etc.; `near` 96 pares únicos; `formula` 36 matches

## Próximo

- [ ] Etapa de PR: `cpcheck diff` no pre-push / pr-review. Sobra de `exact`/`struct` é reportada direto (a prova é o hash).
- [ ] Subagent Haiku (somente leitura) só nos candidatos `near`/`formula`: recebe o par de funções + métricas e responde uma linha ("duplicata real, reusar X" / "coincidência" / "intencional").

## Pendente (decidido em 2026-11-11)

- [ ] **Versão do índice automática.** Hoje o índice só se recria sozinho quando alguém lembra de subir `SCHEMA_VERSION`. Se ajustarem o extrator e esquecerem, as máquinas ficam com impressões digitais velhas e ninguém percebe. Correção: derivar a versão do próprio código do extrator (hash de `extract.py`, `formula.py` e `minhash.py`, guardado numa tabela `meta`) para que qualquer mudança na detecção invalide o índice sozinha.
- [ ] **Distribuição** (decisão da Lua): biblioteca por URL de git ou plugin do Claude Code. Palpite: biblioteca fixada por tag, porque o time já usa `uv` e o ajuste passa pelo mesmo PR e gate dos outros (atualizar = PR que sobe a tag; cada máquina recebe no `uv sync`).
- [ ] **Como propagar ajustes** (depende da distribuição): regra do projeto (`allow`, `exclude`, limiares) vai no `.cpcheck.toml` do wright-apps por PR; mudança na detecção vai no repo do cpcheck, nascendo como teste de regressão (o par que escapou) e saindo em nova versão/tag; o prompt do Haiku mora junto com a ferramenta.
- [ ] **Conjunto rotulado de regressão para o `near`:** guardar os pares do corpus (real / ruído) como teste, para qualquer mudança ser medida (o limiar 0,75 foi calibrado só no wright-apps).
- [ ] **Passo Haiku** (somente leitura, nos candidatos `near`/`formula` e na busca prévia): testar nos 3 casos reais já levantados, dia útil com calendário ANBIMA (`BDay`, `np.busday_*`, `AnbimaCalendar` reimplementados em `mail_saldo`, `mail_movs`, `cotas_email`, pipelines `anbima_*`; canônico é `shared/calendario.py`), `_append_last_date` e `ret_table_data` com `cart_ret_report`. O determinístico decide acima de ~0,85; o Haiku julga a faixa de 0,70 a 0,85 e o que as regras não alcançam (mesma intenção, código diferente).
- [ ] **Memória de vereditos do Haiku** (pares "coincidência"/"intencional") em arquivo versionado no repo, para não repetir falso positivo para todo mundo. Só depois do passo Haiku existir.
- [ ] (Opcional) paralelizar o scan com `multiprocessing` para reduzir o primeiro scan por máquina (~10 s → 2–3 s). Descartados por custo de manutenção: GitHub Action/artefato de CI, índice no repo, blob no Azure.

## Pontas soltas

- [ ] Integração do passo Haiku no pr-review (ver Pendente)
- [ ] Testar o `diff` contra `origin/main` num repo de teste com commits
- [ ] TS/TSX: hoje só Python (no wright-apps o `apps/web` fica de fora)
- [ ] Decidir se o repo fica público ou privado (distribuição: ver Pendente)
- [ ] Decidir adoção em outro repo: hook no `settings.json`, passo no pre-push, ADR

## Fora de escopo (por ora)

GitHub Action, índice compartilhado entre repositórios, duplicidade de dados no Postgres, fórmulas fora de funções, detecção de algoritmos diferentes com o mesmo resultado. Reabrir só se o custo de manutenção compensar.
