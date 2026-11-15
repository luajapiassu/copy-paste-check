# Roadmap

Regra geral: **tudo que dá para provar com regra é determinístico; o LLM só interpreta evidência já calculada** e nunca decide sozinho. Manter simples: pouca dependência, hook rápido, pouco CPU.

## Feito

- [x] Índice incremental em SQLite com `exact` e `struct` (sempre ligados); versão do índice automática (hash do código de detecção + config), então ajuste na detecção recria o índice sozinho
- [x] `formula` (sympy) e `near` (MinHash + nota ponderada por raridade) opcionais, desligados por padrão
- [x] CLI `scan` / `check` / `diff` (working tree vs merge-base, com `--markdown`)
- [x] Hook do Claude Code: bloqueia `exact`/`struct` (PreToolUse), avisa `struct~`/`formula`/`near` (PostToolUse)
- [x] Configuração por repositório (`.cpcheck.toml`), skill para agentes
- [x] Worktree nova herda o índice de outra worktree do mesmo repo; scan em paralelo a partir de 40 arquivos
- [x] Conjunto rotulado de regressão para o `near` (`tests/labeled_near.py`)
- [x] `cpcheck review`: execução das contas puras + busca por vocabulário (FTS5), com termos de busca sugeridos pelo modelo numa chamada curta (liga nome em português a nome/sigla em inglês) + **uma** chamada de julgamento ao Haiku sem ferramentas; respostas guardadas no índice local
- [x] `cpcheck ask`: busca prévia em linguagem natural (Haiku com Grep/Glob/Read)
- [x] `cpcheck bench`: mede o `review` contra um gabarito do próprio repositório (o gabarito não mora aqui)
- [x] CI do próprio repo (pytest em Linux e Windows)

## O que as medições ensinaram

Medido em um repositório Python real (~850 arquivos, ~6.700 funções):

- Scan: primeiro ~6 s com `formula` e `near` ligados, seguintes ~0,1 s; hook ~0,15 s.
- Chamar o modelo com ferramentas (agêntico) custa 29–35 mil tokens por item; busca local + uma chamada sem ferramentas custa ~14 mil tokens para 7 funções novas (~12 s).
- Isolar o CLI (`--strict-mcp-config --disable-slash-commands --setting-sources ""`) derruba uma chamada de ~107 mil para ~1 mil tokens de entrada.
- Sem pensamento o modelo aponta a própria função nova e inventa diferença; com menos de 12 linhas por candidata erra bem mais. Padrão: 12 linhas + pensamento limitado a 2048 tokens.
- Modelo pequeno e modelo médio empataram em acerto no julgamento de pares; o médio custou de 7 a 16 vezes mais.
- `near` acima de 0,85 por regra dava falso positivo em funções com o mesmo esqueleto; só `exact`/`struct` dispensam o modelo.
- Bench com gabarito de 20 casos (15 duplicatas reescritas, 5 parecidas que não são): 12/20 → 20/20 em duas rodadas seguidas, ~55 mil tokens. O que fez a diferença, em ordem:
  - Critério do prompt: "responde à mesma pergunta?" com a lista do que **não** muda a resposta (erro, None, fallback, parâmetro a mais) e do que muda (outra regra/unidade/produto, ler x gravar). Com "substitui com ressalvas pequenas" o mesmo prompt oscilou de 11 a 16 entre rodadas.
  - Mostrar o **final** da candidata: cortar nas 12 primeiras linhas escondia o `return`.
  - Termos de busca do modelo para nome em outro idioma ou sigla.
  - Mais entradas de retornos na execução (conta que só dá número com negativos tinha uma entrada só).
  - Aceitar candidata mostrada em outro item da mesma chamada.

## Pendente

- [ ] Gabarito maior e de mais de um repositório: 20 casos de um repo só, escritos junto com o ajuste do prompt, medem pouco. Par que escapar em PR real vira caso novo.
- [ ] `REVIEW_MIN_TOKENS = 20`: função de uma linha muito curta (`d.weekday() < 5`, 11 tokens) não é checada. Baixar aumenta os itens por chamada; decidir com o gabarito.
- [ ] `ask` sem LLM: a busca por vocabulário já responde "o que existe com essas palavras" sem tokens; medir contra o `ask` agêntico antes de trocar.
- [ ] Execução: só parte das funções puras aceita as entradas fixas (as que recebem DataFrame, colunas ou datas ficam de fora).
- [ ] `near` em pares curtos com cláusula extra fica abaixo do ruído (xfail em `tests/labeled_near.py`).
- [ ] Teste que falhe se uma chamada mínima ao CLI passar de ~5 mil tokens (pega mudança de versão do CLI que quebre o isolamento).
- [ ] TS/TSX: hoje só Python.
- [ ] Distribuição: biblioteca fixada por tag ou plugin do Claude Code.

## Fora de escopo (por ora)

GitHub Action que rode o cpcheck, índice compartilhado entre repositórios, fórmulas fora de funções, detecção de algoritmos diferentes com o mesmo resultado. Reabrir só se o custo de manutenção compensar.
