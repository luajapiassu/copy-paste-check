"""Passo Haiku (somente leitura): julga os candidatos que o determinístico não decide sozinho.

- ``exact``/``struct`` e ``near`` acima de ``judge_high``: duplicata, sem chamar modelo.
- ``near`` entre ``judge_low`` e ``judge_high`` e ``formula``: o Haiku lê as duas funções e dá o veredito.
- ``ask``: busca prévia em linguagem natural; o Haiku usa só Grep/Glob/Read.

Vereditos ficam em ``cpcheck-verdicts.json`` na raiz (versionado), chaveados pelo par de ``h_exact``:
mudou o código de qualquer lado, a chave muda e o par é julgado de novo.
"""

from __future__ import annotations

import json
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


def run_claude(prompt: str, system: str, tools: str | None, cwd: Path | None) -> str:
    """Chama o CLI do Claude Code em modo print. ``tools=None``: sem ferramentas (julgamento puro)."""
    cmd = ["claude", "-p", "--model", MODEL, "--no-session-persistence", "--system-prompt", system,
           "--tools", tools or ""]
    if tools:
        cmd += ["--allowed-tools", tools]
    # sem ferramentas o cwd não importa: roda num diretório vazio para não carregar o CLAUDE.md do projeto
    with tempfile.TemporaryDirectory() as empty:
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                             cwd=cwd if tools else empty, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"claude saiu com {out.returncode}")
    return out.stdout


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
          low: float = 0.70, high: float = 0.85, remember: bool = True) -> list[Verdict]:
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
