# copy-paste-check

Detecta **funções e fórmulas duplicadas** em código Python antes que elas entrem no repositório: no momento em que o agente (Claude Code) escreve o arquivo, e de novo na revisão do PR.

Princípio: **o máximo determinístico, LLM só para o que não dá para decidir com regra.** O caminho rápido (hook e CLI) não chama nenhum modelo e não imprime nada quando não encontra duplicata. Isso mantém o custo de tokens perto de zero.

## Como detecta

Cada função vira um conjunto de impressões digitais, calculadas a partir da AST (não de regex):

| Tipo | Técnica | Pega |
|---|---|---|
| `exact` | hash do corpo (ignora nome, docstring e comentários) | cópia idêntica |
| `struct` | hash da AST com variáveis locais renomeadas (`v0`, `v1`…) e literais trocados pelo tipo | cópia com nomes ou valores trocados |
| `formula` | toda expressão aritmética da função (não só o `return`), simplificada com sympy e invariante a nomes e ordem de variáveis | `(a - b) / b` = `a / b - 1`; `log(a) - log(b)` = `log(a / b)`; `df["close"] / df["close"].shift() - 1` = `precos.close.pct_change()` |
| `near` | MinHash + LSH sobre a sequência de tokens | cópia com algumas linhas alteradas (similaridade ≥ 0,8) |

O índice fica em SQLite (`.cpcheck/index.db` na raiz do repo) e é **incremental**: só reprocessa arquivos cujo tamanho ou data mudou.

Medido num repositório real (~1.050 arquivos Python, ~6.700 funções): primeiro scan em ~15 s, scans seguintes em ~0,1 s e hook em ~0,15 s. Lá ele achou, entre outras, a volatilidade anualizada (`std * √252`) repetida em 3 arquivos, o z-score em 2 e pesos `x / sum(x)` em 2.

Para as fórmulas não virarem ruído, cada operação, função conhecida ou constante não trivial soma peso, e só entram expressões com peso suficiente. Somas simples de variáveis (`a + b + c`) e arquivos de teste ficam de fora.

## Instalação

```bash
uv pip install -e ".[formula]"   # sem [formula] a camada de fórmulas fica desligada
```

## Uso

```bash
cpcheck scan                         # cria/atualiza o índice
cpcheck check shared/calc.py         # checa um arquivo contra o resto do repo
cpcheck diff --base origin/main      # só o que mudou no PR
cpcheck diff --base origin/main --markdown   # saída pronta para comentário de PR
```

`--fail-on strong|any|none` controla o exit code (padrão: `strong`, ou seja, falha só em `exact`/`struct`).

### Hook do Claude Code

Veja [`examples/claude-settings.json`](examples/claude-settings.json). O mesmo comando atende dois eventos:

- **PreToolUse** (Write/Edit/MultiEdit em `.py`): **bloqueia** só quando há evidência forte (`exact`/`struct`) e diz qual função reutilizar.
- **PostToolUse**: **avisa** sem bloquear sobre `formula` e `near`.

Só são checadas funções **novas ou alteradas** pela edição. `CPCHECK_MODE=warn` desliga o bloqueio.

Duplicação intencional (ex.: templates que precisam ser autocontidos): marque a `def` com `# cpcheck: ignore` na mesma linha ou na anterior, ou declare a regra no `.cpcheck.toml`.

Testado em sessão real do Claude Code (`claude -p`): o bloqueio impede a escrita e o agente responde que "`wavg` duplica `media_ponderada` em `shared/calc.py:2`"; o aviso de fórmula chega como contexto depois da escrita. Cada caso custou 2 turnos.

### Configuração

`.cpcheck.toml` na raiz (ou `[tool.cpcheck]` no `pyproject.toml`). Veja [`examples/cpcheck.toml`](examples/cpcheck.toml).

### Skill para agentes

[`skills/copy-paste-check/SKILL.md`](skills/copy-paste-check/SKILL.md) instrui o agente a procurar antes de criar e a decidir o que fazer com uma duplicata. É a parte "LLM" — ela só interpreta o que o CLI já provou.

## Roadmap

Ver [`ROADMAP.md`](ROADMAP.md) — inclui a frente de **duplicidade de dados no Postgres** (reaproveitamento via PK/FK).
