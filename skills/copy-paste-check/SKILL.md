---
name: copy-paste-check
description: Evita duplicar funções, fórmulas e lógica já existentes no repo. Use antes de criar um helper, parser, cálculo financeiro ou utilitário novo, e quando o hook cpcheck bloquear ou avisar sobre uma duplicata.
---

# copy-paste-check

O CLI `cpcheck` já prova a duplicata de forma determinística. Seu papel é só decidir o que fazer com ela. Não releia o repositório inteiro: confie no resultado e abra apenas a função apontada.

## Antes de criar uma função utilitária

1. Escreva a função num arquivo temporário e rode `cpcheck check <arquivo>`. Se não houver saída, não existe duplicata e você pode seguir.
2. Se houver saída, abra só a função indicada (`caminho:linha`) e siga a tabela abaixo.

## Quando o hook bloquear ou avisar

| Tipo | Significa | Ação padrão |
|---|---|---|
| `exact` / `struct` | mesma função, talvez com nomes trocados | importe a existente; não recrie |
| `struct~` | mesma estrutura, valores diferentes (colunas, janelas, constantes) | parametrize a existente e chame-a; se forem coisas distintas, siga |
| `formula` | mesma conta escrita de outro jeito | importe a existente; se a semântica divergir (ex.: unidade, base 252 × 365), explique isso num comentário |
| `near` | parecida, com linhas diferentes | se a diferença for um parâmetro, generalize a existente; senão, siga |

Se a existente estiver num lugar ruim para importar (ex.: dentro de um template ou de um teste), mova-a para um módulo compartilhado e faça os dois pontos importarem de lá.

Só use `# cpcheck: ignore` na `def` quando a duplicação for **intencional** (ex.: arquivos que precisam ser autocontidos) e diga o motivo no mesmo comentário.

## Revisão de PR

Rode `cpcheck diff --base origin/main --markdown` e inclua a saída na revisão. Para cada item, diga em uma linha se é para reutilizar, generalizar ou se é intencional.

## Passo Haiku (só leitura)

- Antes de escrever: `cpcheck ask "dias úteis entre datas" "tirar acento"` lista, por item, as funções do repositório com essas palavras no nome, na docstring ou no corpo (sem modelo, zero tokens). A busca casa palavra igual: se não achar, repita com o termo em inglês ou a sigla (`business days`, `moic`). Abra a candidata antes de reutilizar.
- No PR: `cpcheck review --base origin/main`. Uma linha por função nova: `-> caminho:linha nome` (já existe: reutilize, olhando a ressalva) ou `-> NAO EXISTE`. `[execucao]` = mesmo resultado nas entradas de teste (conta ou texto), provado sem modelo; `[descartada]` = o modelo não respondeu ou respondeu fora das candidatas: confira à mão.
- Para mover uma função para um módulo compartilhado, apague a original antes de escrever a nova (senão o hook bloqueia como cópia).
