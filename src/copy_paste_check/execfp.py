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
             "fromfile", "system", "memmap", "genfromtxt", "fromregex", "DataSource"}
# pandas grava com to_*: só estes to_* são conversão em memória
SAFE_TO = {"to_numpy", "to_list", "to_frame", "to_series", "to_dict", "to_period", "to_timestamp", "to_datetime",
           "to_numeric", "to_timedelta", "to_pydatetime", "to_records", "to_flat_index"}
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
                          ast.AsyncFunctionDef, ast.ClassDef, ast.While)):
            # ponytail: While fora porque um laço sem fim derruba o lote inteiro (um timeout só);
            # `for` em range gigante ainda passa. Se acontecer, um subprocesso por função.
            return False
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in local | SAFE_BUILTINS | SAFE_MODULES:
            return False
        if isinstance(n, ast.Attribute) and (n.attr in BAD_ATTRS or n.attr.startswith(("_", "read_", "save", "load"))
                                             or (n.attr.startswith("to_") and n.attr not in SAFE_TO)):
            return False
    return any(isinstance(n, ast.BinOp) or (isinstance(n, ast.Attribute) and n.attr in MATH_ATTRS)
               for n in ast.walk(node))


RUNNER = r'''
import itertools, json, math, sys
import numpy as np, pandas as pd
rng = np.random.default_rng(0)
P = pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, 300)), index=pd.bdate_range("2023-01-02", periods=300))
R = P.pct_change().dropna()
R2 = pd.Series(rng.normal(0.0002, 0.02, 250), index=pd.bdate_range("2022-01-03", periods=250))
FIRST = {"P": P, "R": R, "R2": R2, "Ra": R.to_numpy(), "Pa": P.to_numpy(), "Rl": R.tolist(), "x": 0.05}
REST = {"None": None, "21": 21, "252": 252, "x": 0.05, "P": P, "R": R}

def canon(v):
    if isinstance(v, (bool, np.bool_)) or v is None:
        return None
    if isinstance(v, (int, float, np.integer, np.floating)):
        v = float(v)
        return [round(v, 9)] if math.isfinite(v) else None
    if isinstance(v, (pd.Series, np.ndarray, list)):
        a = np.asarray(pd.Series(v, dtype="float64").dropna())
        if not a.size:
            return None
        # tamanho + soma + soma ponderada pela posição: só a soma confundia zscore com demean (ambas 0)
        # e sort(x) com x
        return [float(a.size), round(float(a.sum()), 7), round(float(a @ np.arange(1, a.size + 1)) / a.size, 7)]
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
    """Python do projeto analisado (tem numpy/pandas): ``.venv`` da raiz ou, em worktree, a do checkout principal."""
    bases = [root]
    try:
        common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if common:
            bases.append(Path(common).parent)
    except (OSError, subprocess.SubprocessError):
        pass
    for base in bases:
        for p in (base / ".venv" / "Scripts" / "python.exe", base / ".venv" / "bin" / "python"):
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


def _run(funcs: list, python: str) -> dict[str, dict[str, list[float]]]:
    """Etapa que não roda avisa no stderr: "não checou" não pode parecer "checou e não achou"."""
    with tempfile.TemporaryDirectory() as empty:
        try:
            res = subprocess.run([python, "-I", "-c", RUNNER], input=json.dumps(funcs), capture_output=True,
                                 encoding="utf-8", errors="replace", timeout=120, cwd=empty)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"cpcheck: execução pulada ({type(e).__name__}: {e})", file=sys.stderr)
            return {}
    if res.returncode != 0 or not res.stdout.strip():
        why = (res.stderr.strip().splitlines() or ["sem saída"])[-1]
        print(f"cpcheck: execução pulada ({python} sem numpy/pandas?): {why}", file=sys.stderr)
        return {}
    return json.loads(res.stdout)


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
            if any(len(fps[key][k]) != len(fp[k]) for k in common):  # escalar x série: não é a mesma conta
                continue
            a = [x for k in common for x in fps[key][k]]
            b = [x for k in common for x in fp[k]]
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
