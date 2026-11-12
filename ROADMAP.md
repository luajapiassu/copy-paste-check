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

- [x] **Versão do índice automática.** Hoje o índice só se recria sozinho quando alguém lembra de subir `SCHEMA_VERSION`. Se ajustarem o extrator e esquecerem, as máquinas ficam com impressões digitais velhas e ninguém percebe. Correção: derivar a versão do próprio código do extrator (hash de `extract.py`, `formula.py` e `minhash.py`, guardado numa tabela `meta`) para que qualquer mudança na detecção invalide o índice sozinha. **Feito (2026-11-11):** hash de extract/formula/minhash/index + config (min_tokens, formula, near) na tabela `meta`.
- [ ] **Distribuição** (decisão da Lua): biblioteca por URL de git ou plugin do Claude Code. Palpite: biblioteca fixada por tag, porque o time já usa `uv` e o ajuste passa pelo mesmo PR e gate dos outros (atualizar = PR que sobe a tag; cada máquina recebe no `uv sync`).
- [ ] **Como propagar ajustes** (depende da distribuição): regra do projeto (`allow`, `exclude`, limiares) vai no `.cpcheck.toml` do wright-apps por PR; mudança na detecção vai no repo do cpcheck, nascendo como teste de regressão (o par que escapou) e saindo em nova versão/tag; o prompt do Haiku mora junto com a ferramenta.
- [x] **Conjunto rotulado de regressão para o `near`:** guardar os pares do corpus (real / ruído) como teste, para qualquer mudança ser medida (o limiar 0,75 foi calibrado só no wright-apps). **Feito (2026-11-11):** `tests/labeled_near.py` + `test_near_labeled.py`. Achou um bug real (parâmetro a mais renumerava os locais nos tokens) e um limite conhecido (par curto com cláusula extra, xfail).
- [x] **Passo Haiku** (somente leitura, nos candidatos `near`/`formula` e na busca prévia): testar nos 3 casos reais já levantados, dia útil com calendário ANBIMA (`BDay`, `np.busday_*`, `AnbimaCalendar` reimplementados em `mail_saldo`, `mail_movs`, `cotas_email`, pipelines `anbima_*`; canônico é `shared/calendario.py`), `_append_last_date` e `ret_table_data` com `cart_ret_report`. O determinístico decide acima de ~0,85; o Haiku julga a faixa de 0,70 a 0,85 e o que as regras não alcançam (mesma intenção, código diferente). **Feito (2026-11-11):** `cpcheck judge` e `cpcheck ask` (`judge.py`). Dos 3 casos: `ret_table_data` x `cart_ret_report` → coincidência (ddof 1 x 0, composição x pct_change); `_append_last_date` → duplicata por regra; dia útil ANBIMA só o `ask` alcança (aponta `shared/calendario.py`), o `judge` não vê porque o código é diferente. Near ≥0,85 por regra dava falso positivo (e-mails de envio) → só exact/struct e near ≥0,95 pulam o Haiku.
- [x] **Memória de vereditos do Haiku** (pares "coincidência"/"intencional") em arquivo versionado no repo, para não repetir falso positivo para todo mundo. Só depois do passo Haiku existir. **Feito (2026-11-11):** `cpcheck-verdicts.json` na raiz do projeto, chave = par de `h_exact` (qualquer mudança de código refaz o veredito).
- [x] (Opcional) paralelizar o scan com `multiprocessing` para reduzir o primeiro scan por máquina (~10 s → 2–3 s). Descartados por custo de manutenção: GitHub Action/artefato de CI, índice no repo, blob no Azure. **Feito (2026-11-11):** `ProcessPoolExecutor` a partir de 40 arquivos; primeiro scan do corpus 7,1 s → 3,2 s, mesmos 358 pares.

## Achados da simulação em PR real (2026-11-12) e do teste de modelos

Rodado no wright-apps (branch sem commits, 20 `.py` mexidos, 36 funções novas/alteradas, índice de 847 arquivos, ~37 s, 3 chamadas ao Haiku). Resolver um por um:

- [x] **`diff` não vê working tree.** Só olha commits contra a base; na simulação precisei de script próprio (working tree vs `HEAD`). Adicionar `--worktree` (e arquivos não rastreados como "antes vazio") ao `cpcheck diff`/`judge`. **Feito (2026-11-12):** sem flag, virou o padrão: `diff`/`judge` comparam a working tree (commits + não commitado + não rastreado) com o merge-base de `--base`; no CI working tree = HEAD, nada muda. De quebra: `git show` com acento no Windows voltava `None` (cp1252) e o scan descia em `.worktrees/*` (tudo virava exact 1,00); agora pasta com `.git` própria fica fora. No wright-apps reproduziu os 4 candidatos da simulação em 5,5 s.
- [ ] **Teste vs teste vira ruído.** Dos 4 candidatos, 2 eram testes irmãos no mesmo arquivo (`test_desbloquear_*`), julgados "coincidência" depois de gastar chamada. Avaliar não julgar (ou nem indexar no `near`) funções `test_*` do mesmo arquivo; helpers de teste (`_pdf_aberto`) continuam valendo.
- [ ] **Achado real a devolver ao PR do wright-apps:** `_pdf_aberto()` em `apps/api/tests/test_extratos_router.py:436` é cópia de `_pdf_valido()` em `tests/unit/shared/test_relatorios_clientes.py:201` (PDF em branco 100x100 com pypdf). Mover para helper/conftest compartilhado. Fora do cpcheck, anotado aqui por ser o caso que a simulação achou.
- [ ] **Haiku conservador demais.** Bloco OAuth + `sendMail` repetido em `send_movs_email`/`send_marketwatch_email`/`send_mmreport_email` saiu "coincidência" (nota 0,88-0,91), mas é duplicação real do bloco. Prompt pede conservadorismo; considerar veredito `parcial` (trecho comum extraível) e afrouxar.
- [ ] **Mesma intenção, código diferente** (dia útil ANBIMA em `mail_saldo`/`mail_movs`/`cotas_email`) só o `ask` alcança; o `judge` não vê. Decidir se o hook/pr-review roda `ask` por função nova (custo ~US$ 0,003 no Haiku, ~17 s) ou só sob pedido.
- [ ] **Limite do `near` em pares curtos:** par com cláusula extra fica em 0,60 (ruído chega a 0,71); xfail em `tests/labeled_near.py`. Ideias: shingles menores para função curta, ou subir o piso do ruído com mais pares rotulados.
- [ ] **Modelo padrão e amostra maior.** Haiku 5.5 x Sonnet 5.5 empataram (15/16 e 3/3), Sonnet custa 7x no `judge` e 16x no `ask`; cada um oscilou 1 de 8 pares entre repetições. Rodar 30+ pares rotulados do corpus (`cpc/bench.py`) antes de cravar o padrão; hoje `CPCHECK_MODEL` ou `haiku`.
- [ ] **Isolamento do CLI depende de flags do `claude`** (`--strict-mcp-config --disable-slash-commands --setting-sources ""`): sem eles, ~107 mil tokens de entrada por chamada. Teste que falha se a chamada "oi" passar de ~5 mil tokens, para pegar mudança de versão do CLI.
- [ ] **Latência do `judge`** ~8 s por par ambíguo, ~15-20 s com CLI frio: avaliar chamadas em paralelo (hoje sequencial).

## Pontas soltas

- [ ] Integração do passo Haiku no pr-review (ver Pendente)
- [ ] Testar o `diff` contra `origin/main` num repo de teste com commits
- [ ] TS/TSX: hoje só Python (no wright-apps o `apps/web` fica de fora)
- [ ] Decidir se o repo fica público ou privado (distribuição: ver Pendente)
- [ ] Decidir adoção em outro repo: hook no `settings.json`, passo no pre-push, ADR

## Fora de escopo (por ora)

GitHub Action, índice compartilhado entre repositórios, duplicidade de dados no Postgres, fórmulas fora de funções, detecção de algoritmos diferentes com o mesmo resultado. Reabrir só se o custo de manutenção compensar.
