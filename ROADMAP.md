# Roadmap

Regra geral: **tudo que dá para provar com regra é determinístico; o LLM só interpreta evidência já calculada** e nunca decide sozinho.

## 1. Código (feito)

- [x] Índice incremental em SQLite com os tipos `exact`, `struct`, `formula` e `near`
- [x] CLI `scan` / `check` / `diff` (com `--markdown` para PR)
- [x] Hook do Claude Code (bloqueia em PreToolUse, avisa em PostToolUse) com resposta de poucas linhas
- [x] Skill para agentes
- [ ] Fórmulas dentro de qualquer expressão, não só no `return` (ex.: `df["ret"] = df.close / df.close.shift() - 1`)
- [ ] Equivalências de pandas/numpy (`np.log` = `math.log`, `.pct_change()` = `x / x.shift() - 1`)
- [ ] Configuração por repositório (`.cpcheck.toml`): exclusões, `min_tokens`, limiar `near`
- [ ] GitHub Action pronta, que comenta o resultado do `cpcheck diff --markdown` no PR
- [ ] Índice compartilhado entre repositórios (Postgres), para achar duplicatas entre projetos

## 2. Banco de dados: duplicidade de dados e reaproveitamento por PK/FK

Objetivo: quando o agente criar ou alterar uma tabela (model SQLAlchemy, migration Alembic, `CREATE TABLE`, `to_sql`), verificar se aqueles dados já existem e sugerir reaproveitamento via FK em vez de duplicar.

### Determinístico

**Estático**, sem conectar no banco: roda no hook, em milissegundos.

| Verificação | Como |
|---|---|
| Tabela nova parecida com uma existente | compara colunas normalizadas (`snake_case`, sem prefixos como `tb_`, `cod_`, `id_`) e tipos, a partir dos models e das migrations |
| FK faltando | coluna com o mesmo nome e tipo da PK de outra tabela, mas sem `ForeignKey` |
| Coluna repetida | a mesma coluna "de negócio" (ex.: `cnpj`, `isin`, `ticker`) em N tabelas, em vez de uma FK para a tabela dona |

**Perfil dos dados**: só no sandbox, nunca em produção. Roda num job agendado e grava no índice.

| Verificação | Como |
|---|---|
| Mesmo conteúdo em colunas diferentes | assinatura MinHash dos valores distintos de cada coluna; guarda só hashes, nunca os dados |
| FK implícita | dependência de inclusão: todos os valores de `A.x` existem em `B.y` |
| PK candidata | coluna (ou par de colunas) única e sem nulos |
| Linhas duplicadas | hash da linha normalizada, mais comparação aproximada nas chaves de negócio |
| Tabela derivada | a tabela nova é igual a um `JOIN`/`GROUP BY` de tabelas existentes, então deveria ser uma view |

### LLM, só por cima da evidência

- Sinônimos de nomes quando os números indicam a mesma coisa (`cod_cliente` ~ `client_id`)
- Escolher entre reaproveitar, criar FK, transformar em view ou criar tabela nova, citando as métricas
- Escrever a migration corrigida

### Integração

- Hook em `database/models/*.py`, `alembic/versions/*.py` e `*.sql`
- `cpcheck db profile` (job) e `cpcheck db check <migration>` (hook/PR)
