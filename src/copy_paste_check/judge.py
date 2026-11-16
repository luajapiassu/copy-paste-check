"""Passo Haiku (somente leitura): decide o que o determinístico não decide sozinho.

- ``review``: uma chamada sem ferramentas por PR, com cada função nova e as candidatas achadas localmente
  (pares do índice, busca por vocabulário e "quase igual" da execução).
- ``ask --llm``: busca prévia agêntica; o Haiku usa só Grep/Glob/Read (o ``ask`` padrão não chama modelo).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .config import is_test_path
from .extract import FunctionInfo, _words, extract_functions
from .index import Index, Match

MODEL = "haiku"

ASK_SYSTEM = (
    "Você procura, num repositório Python, funções que já fazem o que o usuário vai precisar escrever. "
    "Use Grep com termo específico (sem varrer pastas), olhe shared/ primeiro, abra só candidatas (~20 linhas). "
    "Saída: uma linha por item: '<n> -> [USAR arq:linha assinatura | NAO EXISTE] - como chamar / ressalva "
    "(diferenças que mudam o resultado)'. Inclua one-liners (tirar acento, é dia útil?)."
)

Runner = Callable[[str, str, str | None, Path | None], str]  # (prompt, system, tools|None, cwd) -> texto


USAGE: list[dict] = []  # uma entrada por chamada: tokens, custo e tempo (para medir modelo x custo)


def run_claude(prompt: str, system: str, tools: str | None, cwd: Path | None) -> str:
    """Chama o CLI do Claude Code em modo print. ``tools=None``: sem ferramentas (julgamento puro)."""
    # Os três flags de isolamento cortam ~107k tokens de entrada por chamada (MCPs e settings do usuário
    # eram carregados mesmo com --tools ""): "oi" passou de 107k para ~900 tokens.
    cmd = ["claude", "-p", "--model", os.environ.get("CPCHECK_MODEL", MODEL), "--output-format", "json",
           "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
           "--setting-sources", "", "--system-prompt", system, "--tools", tools or ""]
    if tools:
        cmd += ["--allowed-tools", tools]
    # sem ferramentas o cwd não importa: roda num diretório vazio para não carregar o CLAUDE.md do projeto
    with tempfile.TemporaryDirectory() as empty:
        # sem pensamento o Haiku aponta a própria função e erra ressalva. Teto de 2048 fazia o modelo raciocinar
        # na resposta e se contradizer ("NAO EXISTE - a mais próxima faz o mesmo"): em 2 gabaritos, 7-11/11 e
        # 17/20 com 2048 contra 9-11/11 e 18-20/20 com 8000, com o mesmo total de tokens (2026-11-16)
        env = {**os.environ, "MAX_THINKING_TOKENS": os.environ.get("CPCHECK_THINKING", "8000")}
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                             cwd=cwd if tools else empty, encoding="utf-8", env=env)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"claude saiu com {out.returncode}")
    data = json.loads(out.stdout)
    if data.get("is_error"):  # limite de uso etc. vem com exit 0: não é resposta do modelo
        raise RuntimeError(str(data.get("result") or "claude devolveu erro"))
    u = data.get("usage", {})
    USAGE.append({"model": next(iter(data.get("modelUsage", {})), "?"), "input": u.get("input_tokens", 0),
                  "cache_write": u.get("cache_creation_input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                  "output": u.get("output_tokens", 0), "usd": data.get("total_cost_usd", 0.0),
                  "ms": data.get("duration_ms", 0)})
    return data.get("result", "")


def cached(idx: Index, runner: Runner = run_claude) -> Runner:
    """Mesma pergunta (modelo + sistema + prompt) não paga duas vezes: rodar o review de novo sem mudar nada é grátis.

    ponytail: guarda na tabela ``meta`` do índice local, sem expiração; some quando o índice é recriado.
    """
    def run(prompt: str, system: str, tools: str | None, cwd: Path | None) -> str:
        raw = f"{os.environ.get('CPCHECK_MODEL', MODEL)}\0{system}\0{prompt}".encode()
        key = "llm:" + hashlib.blake2b(raw, digest_size=16).hexdigest()
        row = idx.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        if row:
            return row[0]
        text = runner(prompt, system, tools, cwd)
        if text.strip():
            idx.db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (key, text))
            idx.db.commit()
        return text
    return run


@lru_cache(maxsize=256)
def _parsed(file: str, stamp: int) -> tuple[list[str], dict[str, FunctionInfo]]:
    """Um parse por arquivo por rodada (``stamp`` invalida se o arquivo mudar); antes era um por candidata."""
    source = Path(file).read_text(encoding="utf-8", errors="replace")
    funcs: dict[str, FunctionInfo] = {}
    for f in extract_functions(source, file, 1, False):
        funcs.setdefault(f.qualname, f)
    return source.splitlines(), funcs


def _find(root: Path, path: str, qualname: str) -> tuple[FunctionInfo, str] | None:
    file = root / path
    if not file.exists():
        return None
    lines, funcs = _parsed(str(file), file.stat().st_mtime_ns)
    f = funcs.get(qualname)
    return (f, "\n".join(lines[f.lineno - 1 : f.end_lineno])) if f else None


def ask(root: Path, wanted: list[str], runner: Runner = run_claude) -> str:
    """Busca prévia: uma linha por item pedido, em linguagem natural."""
    prompt = "Itens que vou precisar:\n" + "\n".join(f"{i}. {w}" for i, w in enumerate(wanted, 1))
    return runner(prompt, ASK_SYSTEM, "Grep,Glob,Read", root)


# ---- chamada única: pares do índice + busca por vocabulário + execução --------------------------------

REVIEW_SYSTEM = (
    "Você recebe funções NOVAS de um PR e, para cada uma, funções que JÁ EXISTEM no repositório (candidatas "
    "achadas por similaridade de código, de vocabulário e de resultado). Para cada nova, decida se alguma candidata "
    "RESPONDE À MESMA PERGUNTA: quem fosse chamar a nova poderia chamar a existente e obter a mesma informação. "
    "Continua sendo a mesma (responda USAR e ponha a diferença na ressalva) quando só muda: tratamento de erro, de "
    "None ou de entrada vazia; fallback; nome, ordem ou um parâmetro a mais; tipo equivalente do retorno; o jeito "
    "de escrever a conta; de onde vem um dado auxiliar (ex.: a lista de feriados). "
    "É outra coisa (responda NAO EXISTE e cite a mais próxima na ressalva) quando, para a mesma entrada, o número "
    "ou o efeito é outro: outra regra, outra unidade ou outro produto (dias corridos x dias úteis, semanal x "
    "diário, renda fixa x fundo), outra operação sobre o mesmo dado (ler x gravar, criar x apagar, listar x "
    "buscar um), outra entidade ou tabela, ou só o mesmo esqueleto (CRUD, endpoint, delete+404) sobre coisas "
    "diferentes. A própria função nova nunca é resposta. Saída: uma linha por item, exatamente: "
    "'<n> -> USAR arq:linha nome - ressalva' ou '<n> -> NAO EXISTE - o que a mais próxima faz de diferente' (até 20 "
    "palavras). Decida assim: ache a candidata mais próxima e diga que diferença ela tem no resultado ou no efeito; "
    "se a diferença é das que não contam, ou se não há diferença (você escreveria 'faz o mesmo', 'idêntica', 'mesma "
    "regra'), o veredito é USAR. A linha começa pelo veredito final; não escreva USAR se a conclusão for que é "
    "outra coisa, nem NAO EXISTE se a mais próxima faz a mesma coisa. Responda direto, sem preâmbulo."
)
REVIEW_LINES = 12  # linhas de cada candidata no prompt; com 6 o Haiku errou 4/9 (2026-11-12)
# ponytail: abaixo disso é getter. Medido: "tirar acento" = 30 tokens, dia útil com feriados = 20,
# `d.weekday() < 5` = 11. Com 10 entram ~8% mais funções (268 de 3.175 num repositório real).
REVIEW_MIN_TOKENS = 10


@dataclass(slots=True)
class Finding:
    path: str
    qualname: str
    lineno: int
    canonica: Match | None
    ressalva: str = ""
    origem: str = "haiku"  # haiku | execucao | descartada


def _is_test(qualname: str) -> bool:
    return qualname.rsplit(".", 1)[-1].startswith("test")


def _parse_review(text: str) -> dict[int, tuple[str, str]]:
    """``{item: (escolha, ressalva)}``. Última linha de cada item vence (o modelo às vezes se corrige no meio).

    O formato varia entre chamadas (``1 -> [USAR …] - …``, ``1. nome -> [USAR …]``, ``1 -> NAO EXISTE - …``,
    seta ``→``, travessão): vale qualquer linha numerada cuja escolha tenha USAR ou EXISTE.
    """
    out = {}
    # linha numerada, ou correção no meio do texto ("Veredito: 8 -> NAO EXISTE - …"); na ordem do texto
    found = [*re.finditer(r"^\W*(\d+)\W*\s+(.*)$", text, re.M),
             *re.finditer(r"(?<=[ :])(\d+)\s*(?:->|→|=>)(.*)$", text, re.M)]
    for m in sorted(found, key=lambda m: m.start()):
        rest = re.sub(r"\s[–—]\s", " - ", m.group(2))
        rest = re.split(r"->|→|=>", rest)[-1]
        choice, _, why = rest.partition(" - ")
        choice = choice.strip(" []*")
        if re.search(r"USAR|EXISTE", _sem_acento(choice)):
            out[int(m.group(1))] = (choice, why.strip())
    return out


def _pick(choice: str, cands: list[Match]) -> Match | None:
    """Só aceita candidata listada daquele item (guarda contra apontar a própria função ou inventar)."""
    norm = choice.replace("\\", "/")
    hits = [c for c in cands if re.search(rf"\b{re.escape(c.qualname.rsplit('.', 1)[-1])}\b", norm)]
    return (next((c for c in hits if c.path in norm), hits[0] if len(hits) == 1 else None)
            or next((c for c in cands if f"{c.path}:{c.lineno}" in norm), None))  # só arquivo:linha, sem o nome


def _excerpt(src: str, n: int) -> str:
    """Primeiras ``n`` linhas com a docstring reduzida à primeira frase (docstring longa comia o código)."""
    lines = src.splitlines()
    try:
        fn = ast.parse(textwrap.dedent(src)).body[0]
        doc = ast.get_docstring(fn)
    except (SyntaxError, IndexError, TypeError):
        doc = None
    if doc:
        node = fn.body[0]
        indent = re.match(r"\s*", lines[node.lineno - 1]).group()
        lines[node.lineno - 1 : node.end_lineno] = [f'{indent}"""{doc.strip().splitlines()[0]}"""']
    if len(lines) > n:  # o return fica no fim: cortar só o começo escondia a conta
        lines = lines[: n - 3] + ["    ..."] + lines[-2:]
    return "\n".join(lines)


def _ask_items(idx: Index, asks: list, runner: Runner) -> dict[int, tuple[str, str]]:
    parts = [f"São {len(asks)} itens: responda TODOS, de 1 a {len(asks)}, uma linha cada."]
    for i, (rel, f, cands) in enumerate(asks, 1):
        mine = _find(idx.root, rel, f.qualname)
        blocks = []
        for c in cands:
            got = _find(idx.root, c.path, c.qualname)
            hint = " (rodando as duas em entradas de teste, o resultado é quase igual)" if c.kind == "execucao~" else ""
            blocks.append(f"- {c.path}:{c.lineno} {c.qualname}{hint}\n```python\n"
                          f"{_excerpt(got[1], REVIEW_LINES) if got else ''}\n```")
        parts.append(f"## {i}. NOVA: {f.qualname}\n```python\n{(mine[1] if mine else '')[:1500]}\n```\n"
                     "Candidatas existentes:\n" + "\n".join(blocks))
    return _parse_review(runner("\n\n".join(parts), REVIEW_SYSTEM, None, None))


EXPAND_SYSTEM = (
    "Você recebe funções NOVAS de um PR. Para cada uma, liste até 8 termos de busca (palavras soltas, minúsculas) "
    "que apareceriam no NOME ou na docstring de uma função equivalente que já exista no repositório: o nome "
    "técnico da conta ou operação em inglês e em português, siglas consagradas e sinônimos. Não repita os nomes "
    "de variável da própria função. Saída: uma linha por item, exatamente: '<n>: termo, termo, termo'. Sem preâmbulo."
)


def _expand(idx: Index, items: list, runner: Runner) -> dict[int, list[str]]:
    """Termos de busca dados pelo modelo: ligam nome em português a nome/sigla em inglês (e vice-versa).

    A busca por vocabulário só casa palavra igual; aqui o modelo só sugere onde procurar, e o que ele
    sugerir ainda tem de existir no índice e passar pelo julgamento. Falhou: segue sem os termos.
    """
    parts = []
    for i, (rel, f) in enumerate(items, 1):
        mine = _find(idx.root, rel, f.qualname)
        parts.append(f"## {i}. {f.qualname}\n```python\n{(mine[1] if mine else '')[:800]}\n```")
    try:
        text = runner("\n\n".join(parts), EXPAND_SYSTEM, None, None)
    except (RuntimeError, ValueError, subprocess.SubprocessError, OSError):
        return {}
    return {int(m.group(1)): _words(m.group(2))[:8] for m in re.finditer(r"^\W*(\d+)\W+(.+)$", text, re.M)}


def _sem_acento(s: str) -> str:
    return s.upper().replace("Ã", "A")


def review(idx: Index, changed, runner: Runner = run_claude, k: int = 8, execute: bool = True,
           novas: set[tuple[str, str]] | None = None, expand: bool = False) -> list[Finding]:
    """``changed``: [(path, [FunctionInfo novas/alteradas])]. Uma chamada ao Haiku, sem ferramentas.

    Teste (``test_*``) fica de fora; helper de teste entra. Item que a execução já decidiu não vai ao modelo.
    Busca por vocabulário só para função **nova** (``novas``; None = todas) fora de arquivo de teste: duplicata
    de função alterada já existia antes do PR, então ela só vai ao modelo se o índice achar par.
    ``expand``: uma chamada curta antes pede termos de busca ao modelo (nome em outro idioma, sigla).
    """
    items = [(rel, f) for rel, funcs in changed for f in funcs
             if not _is_test(f.qualname) and f.n_tokens >= REVIEW_MIN_TOKENS]
    pr = {(rel, f.qualname) for rel, funcs in changed for f in funcs}
    out: list[Finding] = []
    quase: dict[tuple[str, str], list[Match]] = {}
    if execute and items:
        from . import execfp  # import tardio: só quem executa paga
        news = {rel: (idx.root / rel).read_text(encoding="utf-8", errors="replace") for rel in {r for r, _ in items}}
        paths = [p for (p,) in idx.db.execute("SELECT path FROM files") if not is_test_path(p)]
        by_exec = execfp.matches(idx.root, paths, news)
        rest = []
        for rel, f in items:
            ms = by_exec.get((rel, f.qualname)) or []
            if ms and ms[0].kind == "execucao":
                out.append(Finding(rel, f.qualname, f.lineno, ms[0], f"mesmo resultado em {int(ms[0].score)} entradas",
                                   "execucao"))
            else:  # "quase igual" (até 5%) é pista, não prova: vai ao modelo como candidata
                quase[(rel, f.qualname)] = ms
                rest.append((rel, f))
        items = rest
    def is_new(rel: str, f: FunctionInfo) -> bool:
        return (novas is None or (rel, f.qualname) in novas) and not is_test_path(rel)

    to_expand = [(rel, f) for rel, f in items if is_new(rel, f)] if expand else []
    termos = _expand(idx, to_expand, runner) if to_expand else {}
    termos = {(rel, f.qualname): termos.get(i, []) for i, (rel, f) in enumerate(to_expand, 1)}
    asks = []
    for rel, f in items:
        seen, cands = set(), []
        nova = is_new(rel, f)
        # o índice já tem a working tree: outra função do próprio PR não é "candidata existente" no vocabulário
        # (confundia o Haiku); cópia entre duas novas continua vindo pelo query (exact/near)
        vocab = [m for m in idx.by_vocab(f, k + len(pr)) if (m.path, m.qualname) not in pr] if nova else []
        extra = termos.get((rel, f.qualname))
        sugeridas = [m for m in idx.by_vocab(f, 4 + len(pr), extra) if (m.path, m.qualname) not in pr][:4] if extra else []
        for m in quase.get((rel, f.qualname), []) + idx.query(f, limit=3) + sugeridas + vocab:
            if (m.path, m.qualname) not in seen and (m.path, m.qualname) != (rel, f.qualname):
                seen.add((m.path, m.qualname))
                cands.append(m)
        cands = cands[: k + len(sugeridas)]  # as sugeridas pelos termos do modelo entram a mais, não no lugar
        if cands:
            asks.append((rel, f, cands))
    if not asks:
        return out
    try:
        answers = _ask_items(idx, asks, runner)
        missing = [i for i in range(1, len(asks) + 1) if i not in answers]
        if missing:  # o modelo às vezes para no meio: uma segunda chamada só com o que faltou
            again = _ask_items(idx, [asks[i - 1] for i in missing], runner)
            answers |= {i: again[j] for j, i in enumerate(missing, 1) if j in again}
    except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as e:
        return out + [Finding(rel, f.qualname, f.lineno, None, f"Haiku indisponível: {e}", "descartada")
                      for rel, f, _ in asks]
    for i, (rel, f, cands) in enumerate(asks, 1):
        choice, why = answers.get(i, ("", "sem resposta"))
        # na ressalva só vale o veredito em maiúsculas ("corrijo: NAO EXISTE"); "o parâmetro não existe" é prosa
        if "NAO EXISTE" in _sem_acento(choice) or re.search(r"N[AÃ]O EXISTE", why):
            out.append(Finding(rel, f.qualname, f.lineno, None, why, "haiku"))
            continue
        # candidata listada em outro item da mesma chamada também vale: existe e o modelo viu o código dela
        pick = _pick(choice, cands) or _pick(choice, [c for _, _, cs in asks for c in cs
                                                      if (c.path, c.qualname) != (rel, f.qualname)])
        if not pick and re.search(rf"\b{re.escape(f.qualname.rsplit('.', 1)[-1])}\b", choice):
            out.append(Finding(rel, f.qualname, f.lineno, None, "apontou a própria função", "haiku"))
            continue
        out.append(Finding(rel, f.qualname, f.lineno, pick, why if pick else f"resposta fora das candidatas: {choice}",
                           "haiku" if pick else "descartada"))
    return out
