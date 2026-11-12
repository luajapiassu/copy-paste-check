"""Impressão digital por execução: mesma conta com nome e código diferentes (sem LLM, determinístico).

Só roda funções de módulo **puras**: o corpo usa apenas nomes locais, ``np``/``pd``/``math`` e builtins
seguros; sem import, IO, ``self``, sessão de banco ou rede. Cada uma roda num subprocesso ``-I`` (com o
Python do projeto analisado, que tem numpy/pandas) em entradas fixas — série de preços com semente fixa,
retornos, janelas None/21/252 — e as saídas são comparadas:

- ``execucao``: mesmo resultado em todas as entradas comuns → mesma conta.
- ``execucao~``: até 5% de diferença em todas → mesma conta com outro parâmetro (ddof, base de dias…).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .index import Match

SAFE_BUILTINS = {"abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len",
                 "list", "map", "max", "min", "pow", "range", "reversed", "round", "set", "sorted", "sum", "tuple",
                 "zip", "str", "ValueError", "ZeroDivisionError", "TypeError", "KeyError", "IndexError", "Exception"}
SAFE_MODULES = {"np", "pd", "math", "numpy", "pandas"}
BAD_ATTRS = {"read_csv", "read_excel", "read_sql", "to_csv", "to_excel", "to_sql", "read_parquet", "to_parquet",
             "read_json", "to_json", "read_pickle", "to_pickle", "load", "save", "savetxt", "loadtxt", "tofile",
             "fromfile", "system", "memmap"}
MATH_ATTRS = {"std", "mean", "pct_change", "cumprod", "var", "sqrt", "log", "diff", "prod", "busday_count"}
NEAR = 0.05  # ponytail: tolerância fixa; ddof em janela de ~15 retornos dá ~3%


def pure(node: ast.AST) -> bool:
    """Função de módulo que só faz conta: pode ser executada sem efeito colateral."""
    if not isinstance(node, ast.FunctionDef) or node.decorator_list:
        return False
    a = node.args
    args = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    if not args or args[0] in ("self", "cls") or a.vararg or a.kwarg:
        return False
    local = set(args) | {n.arg for n in ast.walk(node) if isinstance(n, ast.arg)}
    local |= {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    for n in ast.walk(node):
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Await, ast.Yield, ast.YieldFrom,
                          ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in local | SAFE_BUILTINS | SAFE_MODULES:
            return False
        if isinstance(n, ast.Attribute) and (n.attr in BAD_ATTRS or n.attr.startswith("_")):
            return False
    return any(isinstance(n, ast.BinOp) or (isinstance(n, ast.Attribute) and n.attr in MATH_ATTRS)
               for n in ast.walk(node))


RUNNER = r'''
import itertools, json, math, sys
import numpy as np, pandas as pd
rng = np.random.default_rng(0)
P = pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, 300)), index=pd.bdate_range("2023-01-02", periods=300))
R = P.pct_change().dropna()
FIRST = {"P": P, "R": R, "Pa": P.to_numpy(), "Rl": R.tolist(), "x": 0.05}
REST = {"None": None, "21": 21, "252": 252, "x": 0.05, "P": P, "R": R}

def canon(v):
    if isinstance(v, (bool, np.bool_)) or v is None:
        return None
    if isinstance(v, (int, float, np.integer, np.floating)):
        v = float(v)
        return round(v, 9) if math.isfinite(v) else None
    if isinstance(v, (pd.Series, np.ndarray, list)):
        a = np.asarray(pd.Series(v, dtype="float64").dropna())
        return round(float(a.sum()), 7) if a.size else None
    return None

out = {}
for key, src, nreq, ntot in json.load(sys.stdin):
    ns = {"np": np, "pd": pd, "math": math}
    try:
        exec(src, ns)
        fn = ns[key.rsplit("::", 1)[1]]
    except Exception:
        continue
    fp = {}
    for n in range(max(nreq, 1), min(ntot, 3) + 1):
        for i, combo in enumerate(itertools.product(FIRST.items(), *([REST.items()] * (n - 1)))):
            if i >= 40:
                break
            try:
                with np.errstate(all="ignore"):
                    v = canon(fn(*[c[1] for c in combo]))
            except Exception:
                continue
            if v is not None:
                fp["|".join(c[0] for c in combo)] = v
    if fp:
        out[key] = fp
print(json.dumps(out))
'''


def find_python(root: Path) -> str:
    """Python do projeto analisado (tem numpy/pandas); senão o atual."""
    for p in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def _pure_funcs(rel: str, source: str) -> list[tuple[str, str, int, int, int]]:
    """(rel::nome, código, nº de args obrigatórios, nº de args, linha)"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    lines = source.splitlines()
    out = []
    for node in tree.body:
        if pure(node):
            a = node.args
            n = len(a.posonlyargs + a.args)
            out.append((f"{rel}::{node.name}", "\n".join(lines[node.lineno - 1 : node.end_lineno]), n - len(a.defaults), n,
                        node.lineno))
    return out


def _run(funcs: list, python: str) -> dict[str, dict[str, float]]:
    with tempfile.TemporaryDirectory() as empty:
        try:
            res = subprocess.run([python, "-I", "-c", RUNNER], input=json.dumps(funcs), capture_output=True,
                                 encoding="utf-8", errors="replace", timeout=120, cwd=empty)
        except (OSError, subprocess.SubprocessError):
            return {}
    return json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else {}  # sem numpy/pandas: nada


def matches(root: Path, paths: list[str], new: dict[str, str], python: str | None = None,
            ) -> dict[tuple[str, str], list[Match]]:
    """Compara as funções puras de ``new`` (rel -> código) com as de ``paths`` (já no repo)."""
    mine = [f for rel, src in new.items() for f in _pure_funcs(rel, src)]
    if not mine:
        return {}
    keys = {f[0] for f in mine}
    others = [f for rel in paths if rel not in new and (root / rel).exists()
              for f in _pure_funcs(rel, (root / rel).read_text(encoding="utf-8", errors="replace"))]
    line = {f[0]: f[4] for f in others}
    fps = _run([f[:4] for f in mine + [f for f in others if f[0] not in keys]], python or find_python(root))
    out: dict[tuple[str, str], list[Match]] = {}
    for key in keys & fps.keys():
        for other, fp in fps.items():
            if other in keys:
                continue
            common = [k for k in fps[key] if k in fp]
            if len(common) < 2:
                continue
            a = [fps[key][k] for k in common]
            b = [fp[k] for k in common]
            if all(abs(x - y) <= 1e-7 * max(1.0, abs(x)) for x, y in zip(a, b)):
                kind = "execucao"
            elif all(abs(x - y) <= NEAR * max(1e-12, abs(x)) for x, y in zip(a, b)):
                kind = "execucao~"
            else:
                continue
            rel, name = other.rsplit("::", 1)
            out.setdefault(tuple(key.rsplit("::", 1)), []).append(Match(kind, len(common), rel, name, line[other]))
    for ms in out.values():
        ms.sort(key=lambda m: (m.kind != "execucao", -m.score))
    return out
