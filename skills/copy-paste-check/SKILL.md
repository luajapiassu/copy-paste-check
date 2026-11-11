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
| `formula` | mesma conta escrita de outro jeito | importe a existente; se a semântica divergir (ex.: unidade, base 252 × 365), explique isso num comentário |
| `near` | parecida, com linhas diferentes | se a diferença for um parâmetro, generalize a existente; senão, siga |

Se a existente estiver num lugar ruim para importar (ex.: dentro de um template ou de um teste), mova-a para um módulo compartilhado e faça os dois pontos importarem de lá.

Só use `# cpcheck: ignore` na `def` quando a duplicação for **intencional** (ex.: arquivos que precisam ser autocontidos) e diga o motivo no mesmo comentário.

## Revisão de PR

Rode `cpcheck diff --base origin/main --markdown` e inclua a saída na revisão. Para cada item, diga em uma linha se é para reutilizar, generalizar ou se é intencional.

## Passo Haiku (só leitura)

- Antes de escrever: `cpcheck ask "lista de dias úteis entre datas" "tirar acento"` devolve, por item, a função canônica e as ressalvas que mudam o resultado.
- No PR ou depois do hook: `cpcheck judge` (sem arquivos = o diff contra `--base`). Exact/struct e near ≥ 0,95 são duplicata sem chamar modelo; o resto o Haiku julga e o veredito fica em `cpcheck-verdicts.json` (versionado). `coincidencia` e `intencional` não exigem ação; `duplicata` segue a tabela acima; `incerto` = Haiku indisponível, olhe a função apontada.
