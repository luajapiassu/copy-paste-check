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
- [x] `cpcheck ask`: busca prévia por palavras no índice, sem modelo (`--llm` mantém a busca agêntica com o Haiku)
- [x] Execução também de funções de texto, números, datas e DataFrame: num repositório real a cobertura foi de 21 para 108 das 147 funções puras e achou "só dígitos do CNPJ" em 8 arquivos e "normalizar e-mail" em 3
- [x] Review checa função a partir de 10 tokens (antes 20: one-liner como `d.weekday() < 5` escapava)
- [x] Teste ao vivo do isolamento do CLI (`CPCHECK_LIVE=1 pytest -k isolada`): falha se a chamada mínima passar de 5 mil tokens
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

- Segundo repositório (gabarito de 11 casos, nunca usado para ajustar o prompt): 5/11 na primeira rodada. O modelo respondia "NAO EXISTE - a mais próxima faz o mesmo". Três ajustes levaram a 9–11/11 sem piorar o primeiro (18–20/20): dizer no prompt que sem diferença apontável o veredito é USAR; ler correção feita no meio da resposta; teto de pensamento de 2048 para 8000 (mesmo total de tokens).
- Veredito no fim da linha (`n | diferença | veredito`): o modelo escrevia `NAO EXISTE` no começo e "faz o mesmo" na justificativa (4 de 5 erros em 6 rodadas). Com o veredito depois da diferença, 5 erros em 93 julgamentos caíram para 2 em 93 (amostra pequena). Nenhum alarme falso nas 12 rodadas.
- O julgamento **oscila entre rodadas**: 1 ou 2 casos de fronteira mudam de lado com o mesmo código. Os termos de busca sugeridos pelo modelo também variam, e uma candidata pode não aparecer numa rodada.
- `ask` sem modelo, 9 pedidos em português: a função certa veio entre as 4 primeiras em 6. Erra quando o pedido está em português e o nome em inglês ("é dia útil" x `_is_business_day`).

## Pendente

- [ ] Oscilação do julgamento: medir cada gabarito em 5 rodadas e testar lotes menores ou segunda opinião só nos itens `NAO EXISTE` com candidata forte.
- [ ] Alarme falso (`USAR` apontando função que não serve): não apareceu no bench, mas o gabarito só tem 9 casos negativos escritos à mão. Medir em uso real (anotar cada aviso como usado / falso positivo) e virar caso novo no gabarito cada alarme falso, sobretudo funções de mesmo esqueleto (CRUD, endpoint, leitura de planilha).
- [ ] Gabarito ainda pequeno (20 + 11 casos, dois repositórios do mesmo time). Par que escapar em PR real vira caso novo.
- [ ] `ask`: não cruza português e inglês (o `review` cruza com uma chamada curta ao modelo; o `ask` não, para custar zero).
- [ ] Execução: 39 das 147 funções puras do repositório de teste ainda não rodam (pedem dicionário, lista de registros ou objeto do projeto).
- [ ] `near` em pares curtos com cláusula extra fica abaixo do ruído (xfail em `tests/labeled_near.py`); hoje quem pega esse caso é o `review`.
- [ ] TS/TSX: hoje só Python.
- [ ] Distribuição: hoje é instalar direto do repositório (`uv pip install "git+https://github.com/luajapiassu/copy-paste-check"`); tag ou plugin só se a main instável virar problema.

## Fora de escopo (por ora)

GitHub Action que rode o cpcheck, índice compartilhado entre repositórios, fórmulas fora de funções, detecção de algoritmos diferentes com o mesmo resultado. Reabrir só se o custo de manutenção compensar.
