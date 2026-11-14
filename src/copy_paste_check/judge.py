"""Passo Haiku (somente leitura): julga os candidatos que o determinístico não decide sozinho.

- ``exact``/``struct`` e ``near`` acima de ``judge_high``: duplicata, sem chamar modelo.
- ``near`` entre ``judge_low`` e ``judge_high`` e ``formula``: o Haiku lê as duas funções e dá o veredito.
- ``ask``: busca prévia em linguagem natural; o Haiku usa só Grep/Glob/Read.

Vereditos ficam em ``cpcheck-verdicts.json`` na raiz (versionado), chaveados pelo par de ``h_exact``:
mudou o código de qualquer lado, a chave muda e o par é julgado de novo.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .extract import FunctionInfo, extract_functions
from .index import Index, Match

VERDICTS_FILE = "cpcheck-verdicts.json"
MODEL = "haiku"
VEREDITOS = ("duplicata", "coincidencia", "intencional")

JUDGE_SYSTEM = (
    "Você compara duas funções Python de um mesmo repositório e decide se a segunda é uma duplicata "
    "da primeira (alguém deveria importar uma em vez de manter as duas). Responda SÓ um JSON: "
    '{"veredito": "duplicata" | "coincidencia" | "intencional", "canonica": "caminho:linha da que deve ser usada", '
    '"diferencas": "o que muda o resultado (ddof, feriados, preço x retorno, NaN/índice, unidade); uma frase"}. '
    "coincidencia = só parecem (mesmo esqueleto, intenção diferente); intencional = cópia deliberada "
    "(arquivo autocontido, template). Seja conservador: na dúvida entre duplicata e coincidencia, diga coincidencia."
)
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
        # pensamento limitado: sem ele o Haiku aponta a própria função e erra ressalva; sem teto, 5-7 mil
        # tokens de saída e ~30 s (medido em 2026-11-12, cpc/sim_vocab.py)
        env = {**os.environ, "MAX_THINKING_TOKENS": os.environ.get("CPCHECK_THINKING", "2048")}
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                             cwd=cwd if tools else empty, encoding="utf-8", env=env)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"claude saiu com {out.returncode}")
    data = json.loads(out.stdout)
    u = data.get("usage", {})
    USAGE.append({"model": next(iter(data.get("modelUsage", {})), "?"), "input": u.get("input_tokens", 0),
                  "cache_write": u.get("cache_creation_input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                  "output": u.get("output_tokens", 0), "usd": data.get("total_cost_usd", 0.0),
                  "ms": data.get("duration_ms", 0)})
    return data.get("result", "")


@dataclass(slots=True)
class Verdict:
    path: str
    qualname: str
    lineno: int
    other: Match
    veredito: str
    canonica: str = ""
    diferencas: str = ""
    origem: str = "regra"  # regra | haiku | memoria


def _find(root: Path, path: str, qualname: str) -> tuple[FunctionInfo, str] | None:
    file = root / path
    if not file.exists():
        return None
    source = file.read_text(encoding="utf-8", errors="replace")
    for f in extract_functions(source, path, 1, False):
        if f.qualname == qualname:
            return f, "\n".join(source.splitlines()[f.lineno - 1 : f.end_lineno])
    return None


def _key(a: FunctionInfo, b: FunctionInfo) -> str:
    return ":".join(sorted([a.h_exact, b.h_exact]))  # exato: trocar um literal (ddof 1→0) refaz o veredito


def load_memory(root: Path) -> dict:
    p = root / VERDICTS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_memory(root: Path, mem: dict) -> None:
    text = json.dumps(mem, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (root / VERDICTS_FILE).write_text(text, encoding="utf-8")


def parse_verdict(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    if data.get("veredito") not in VEREDITOS:
        raise ValueError(f"resposta sem veredito válido: {text[:200]!r}")
    return data


def judge(idx: Index, results: list[tuple[str, str, int, list[Match]]], runner: Runner = run_claude,
          low: float = 0.70, high: float = 0.95, remember: bool = True) -> list[Verdict]:
    """``results`` vem de ``check``/``diff``: (path, função, linha, matches)."""
    mem = load_memory(idx.root)
    out: list[Verdict] = []
    dirty = False
    for path, qual, line, matches in results:
        mine = _find(idx.root, path, qual)
        for m in matches:
            if m.kind in ("exact", "struct") or (m.kind == "near" and m.score >= high):
                out.append(Verdict(path, qual, line, m, "duplicata", f"{m.path}:{m.lineno}", origem="regra"))
                continue
            if m.kind == "near" and m.score < low:
                continue
            theirs = _find(idx.root, m.path, m.qualname)
            if not mine or not theirs:
                continue
            key = _key(mine[0], theirs[0])
            if key in mem:
                v = mem[key]
                out.append(Verdict(path, qual, line, m, v["veredito"], v.get("canonica", ""),
                                   v.get("diferencas", ""), "memoria"))
                continue
            prompt = (f"Função A (a canônica candidata) — {m.path}:{m.lineno} {m.qualname}\n```python\n{theirs[1]}\n```\n\n"
                      f"Função B (a nova) — {path}:{line} {qual}\n```python\n{mine[1]}\n```\n\n"
                      f"O detector marcou {m.kind} (nota {m.score:.2f}).")
            try:
                data = parse_verdict(runner(prompt, JUDGE_SYSTEM, None, None))
            except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as e:  # JSONDecodeError é ValueError
                out.append(Verdict(path, qual, line, m, "incerto", diferencas=f"Haiku indisponível: {e}", origem="haiku"))
                continue
            out.append(Verdict(path, qual, line, m, data["veredito"], data.get("canonica", ""),
                               data.get("diferencas", ""), "haiku"))
            if remember:
                mem[key] = {"veredito": data["veredito"], "canonica": data.get("canonica", ""),
                            "diferencas": data.get("diferencas", ""), "a": f"{m.path}::{m.qualname}",
                            "b": f"{path}::{qual}", "data": date.today().isoformat()}
                dirty = True
    if dirty:
        save_memory(idx.root, mem)
    return out


def ask(root: Path, wanted: list[str], runner: Runner = run_claude) -> str:
    """Busca prévia: uma linha por item pedido, em linguagem natural."""
    prompt = "Itens que vou precisar:\n" + "\n".join(f"{i}. {w}" for i, w in enumerate(wanted, 1))
    return runner(prompt, ASK_SYSTEM, "Grep,Glob,Read", root)


# ---- chamada única: pares do índice + busca por vocabulário + execução --------------------------------

REVIEW_SYSTEM = (
    "Você recebe funções NOVAS de um PR e, para cada uma, funções que JÁ EXISTEM no repositório (candidatas "
    "achadas por similaridade de código e de vocabulário). Para cada nova, diga se alguma candidata faz a MESMA "
    "coisa (mesma entrada, mesma saída, mesmo efeito). Mesmo esqueleto (CRUD, endpoint, delete+404) sobre "
    "entidades diferentes NÃO é a mesma coisa. USAR só quando a candidata substitui a nova (trocar uma pela outra "
    "dá o mesmo resultado, ressalvas pequenas). Se só parece, ou é a mais próxima mas faz outra coisa, a resposta é "
    "NAO EXISTE e a mais próxima vai na ressalva. A própria função nova nunca é resposta. Saída: uma linha por "
    "item, exatamente: '<n> -> USAR arq:linha nome - ressalva' ou '<n> -> NAO EXISTE - ressalva' (até 20 "
    "palavras, só diferenças que mudam o resultado). Responda direto, sem preâmbulo."
)
REVIEW_LINES = 12  # linhas de cada candidata no prompt; com 6 o Haiku errou 4/9 (2026-11-12)
REVIEW_MIN_TOKENS = 20  # ponytail: abaixo disso é getter/one-liner; baixar se escapar one-liner duplicado


@dataclass(slots=True)
class Finding:
    path: str
    qualname: str
    lineno: int
    canonica: Match | None
    ressalva: str = ""
    origem: str = "haiku"  # haiku | execucao | execucao~ | descartada


def _is_test(qualname: str) -> bool:
    return qualname.rsplit(".", 1)[-1].startswith("test")


def _parse_review(text: str) -> dict[int, tuple[str, str]]:
    """``{item: (escolha, ressalva)}``. Última linha de cada item vence (o modelo às vezes se corrige no meio).

    O formato varia entre chamadas (``1 -> [USAR …] - …``, ``1. nome -> [USAR …]``, ``1 -> NAO EXISTE - …``,
    seta ``→``, travessão): vale qualquer linha numerada cuja escolha tenha USAR ou EXISTE.
    """
    out = {}
    for m in re.finditer(r"^\W*(\d+)\W*\s+(.*)$", text, re.M):
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
    return next((c for c in hits if c.path in norm), hits[0] if len(hits) == 1 else None)


def _ask_items(idx: Index, asks: list, runner: Runner) -> dict[int, tuple[str, str]]:
    parts = [f"São {len(asks)} itens: responda TODOS, de 1 a {len(asks)}, uma linha cada."]
    for i, (rel, f, cands) in enumerate(asks, 1):
        mine = _find(idx.root, rel, f.qualname)
        blocks = []
        for c in cands:
            got = _find(idx.root, c.path, c.qualname)
            body = "\n".join(got[1].splitlines()[:REVIEW_LINES]) if got else ""
            blocks.append(f"- {c.path}:{c.lineno} {c.qualname}\n```python\n{body}\n```")
        parts.append(f"## {i}. NOVA: {f.qualname}\n```python\n{(mine[1] if mine else '')[:1500]}\n```\n"
                     "Candidatas existentes:\n" + "\n".join(blocks))
    return _parse_review(runner("\n\n".join(parts), REVIEW_SYSTEM, None, None))


def _sem_acento(s: str) -> str:
    return s.upper().replace("Ã", "A")


def review(idx: Index, changed, runner: Runner = run_claude, k: int = 8, execute: bool = True,
           novas: set[tuple[str, str]] | None = None) -> list[Finding]:
    """``changed``: [(path, [FunctionInfo novas/alteradas])]. Uma chamada ao Haiku, sem ferramentas.

    Teste (``test_*``) fica de fora; helper de teste entra. Item que a execução já decidiu não vai ao modelo.
    Busca por vocabulário só para função **nova** (``novas``; None = todas) fora de arquivo de teste: duplicata
    de função alterada já existia antes do PR, então ela só vai ao modelo se o índice achar par.
    """
    items = [(rel, f) for rel, funcs in changed for f in funcs
             if not _is_test(f.qualname) and f.n_tokens >= REVIEW_MIN_TOKENS]
    pr = {(rel, f.qualname) for rel, funcs in changed for f in funcs}
    out: list[Finding] = []
    if execute and items:
        from . import execfp  # import tardio: só quem executa paga
        news = {rel: (idx.root / rel).read_text(encoding="utf-8", errors="replace") for rel in {r for r, _ in items}}
        paths = [p for (p,) in idx.db.execute("SELECT path FROM files") if "test" not in p.rsplit("/", 1)[-1]]
        by_exec = execfp.matches(idx.root, paths, news)
        rest = []
        for rel, f in items:
            ms = by_exec.get((rel, f.qualname))
            if ms:
                out.append(Finding(rel, f.qualname, f.lineno, ms[0], f"mesmo resultado em {int(ms[0].score)} entradas",
                                   ms[0].kind))
            else:
                rest.append((rel, f))
        items = rest
    asks = []
    for rel, f in items:
        seen, cands = set(), []
        nova = (novas is None or (rel, f.qualname) in novas) and "test" not in rel.rsplit("/", 1)[-1]
        # o índice já tem a working tree: outra função do próprio PR não é "candidata existente" no vocabulário
        # (confundia o Haiku); cópia entre duas novas continua vindo pelo query (exact/near)
        vocab = [m for m in idx.by_vocab(f, k + len(pr)) if (m.path, m.qualname) not in pr] if nova else []
        for m in idx.query(f, limit=3) + vocab:
            if (m.path, m.qualname) not in seen and (m.path, m.qualname) != (rel, f.qualname):
                seen.add((m.path, m.qualname))
                cands.append(m)
        if cands[:k]:
            asks.append((rel, f, cands[:k]))
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
        if "NAO EXISTE" in _sem_acento(choice) or "NAO EXISTE" in _sem_acento(why):  # inclui "corrijo: NÃO EXISTE"
            out.append(Finding(rel, f.qualname, f.lineno, None, why, "haiku"))
            continue
        pick = _pick(choice, cands)
        if not pick and re.search(rf"\b{re.escape(f.qualname.rsplit('.', 1)[-1])}\b", choice):
            out.append(Finding(rel, f.qualname, f.lineno, None, "apontou a própria função", "haiku"))
            continue
        out.append(Finding(rel, f.qualname, f.lineno, pick, why if pick else f"resposta fora das candidatas: {choice}",
                           "haiku" if pick else "descartada"))
    return out
