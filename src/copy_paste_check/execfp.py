"""Impressão digital por execução: mesma conta com nome e código diferentes (sem LLM, determinístico).

Só roda funções de módulo **puras**: o corpo usa apenas nomes locais, ``np``/``pd``/``math``/``re``/
``unicodedata`` e builtins seguros; sem import, IO, ``self``, sessão de banco ou rede. Cada uma roda num
subprocesso ``-I`` (com o Python do projeto analisado, que tem numpy/pandas) em entradas fixas — série de
preços com semente fixa, retornos, janelas None/21/252, textos (e-mail, CNPJ, CPF, valor em reais, nome
com acento), números, datas e um DataFrame com as colunas que a função cita — e as saídas são comparadas:

- ``execucao``: mesmo resultado em todas as entradas comuns → mesma conta (ou mesmo tratamento de texto).
- ``execucao~``: até 5% de diferença em todas → mesma conta com outro parâmetro (ddof, base de dias…); ou
  igual onde as duas respondem, mas uma devolve vazio (None/NaN) onde a outra devolve valor.
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
SAFE_MODULES = {"np", "pd", "math", "numpy", "pandas", "re", "unicodedata"}
BAD_ATTRS = {"read_csv", "read_excel", "read_sql", "to_csv", "to_excel", "to_sql", "read_parquet", "to_parquet",
             "read_json", "to_json", "read_pickle", "to_pickle", "load", "save", "savetxt", "loadtxt", "tofile",
             "fromfile", "system", "memmap", "genfromtxt", "fromregex", "DataSource"}
# pandas grava com to_*: só estes to_* são conversão em memória
SAFE_TO = {"to_numpy", "to_list", "to_frame", "to_series", "to_dict", "to_period", "to_timestamp", "to_datetime",
           "to_numeric", "to_timedelta", "to_pydatetime", "to_records", "to_flat_index"}
MATH_ATTRS = {"std", "mean", "pct_change", "cumprod", "var", "sqrt", "log", "diff", "prod", "busday_count"}
# função de texto também se repete (tirar acento, normalizar e-mail, só dígitos do CNPJ)
TEXT_ATTRS = {"strip", "lower", "upper", "casefold", "replace", "normalize", "sub", "split", "join", "zfill"}
NA = "∅"  # a função rodou e devolveu None/NaN/vazio (erro não conta: a entrada é só ignorada)
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
    return any(isinstance(n, ast.BinOp) or (isinstance(n, ast.Attribute) and n.attr in MATH_ATTRS | TEXT_ATTRS)
               for n in ast.walk(node))


RUNNER = r'''
import ast, itertools, json, math, re, sys, unicodedata
import numpy as np, pandas as pd
rng = np.random.default_rng(0)
P = pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, 300)), index=pd.bdate_range("2023-01-02", periods=300))
R = P.pct_change().dropna()
R2 = pd.Series(rng.normal(0.0002, 0.02, 250), index=pd.bdate_range("2022-01-03", periods=250))
FIRST = {"P": P, "R": R, "R2": R2, "Ra": R.to_numpy(), "Pa": P.to_numpy(), "Rl": R.tolist(), "x": 0.05,
         "n1": 1234.567, "n2": -0.0312, "d": pd.Timestamp("2024-03-15"),
         # textos: cada um separa um tratamento (caixa, espaço duplo, acento, pontuação, dígito verificador)
         "s0": "  Fulano.Silva@Exemplo.Com.BR ", "s1": "11.222.333/0001-81", "s2": "529.982.247-25",
         "s3": "R$ 1.234,56", "s4": " São  João (Ação) ", "s5": "11222333000181", "s6": "-12,5%", "s7": "",
         "s8": " " + "Relatório Macro — Out/2026 " * 6}
REST = {"None": None, "21": 21, "252": 252, "x": 0.05, "P": P, "R": R, "d2": pd.Timestamp("2024-06-28")}
COLS = [R2.to_numpy(), np.abs(R2.to_numpy()) * 1e4, P.to_numpy()[:250], np.arange(250.0), -R2.to_numpy()]

def frame(src):
    """DataFrame com as colunas que a função cita (texto entre aspas), sempre os mesmos dados por posição:
    a mesma conta com outros nomes de coluna recebe a mesma entrada."""
    try:
        names = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    except SyntaxError:
        names = []
    names = list(dict.fromkeys(n for n in names if n and len(n) < 40))[:8] or ["a", "b"]
    return pd.DataFrame({n: COLS[i % len(COLS)] for i, n in enumerate(names)}, index=R2.index)

def canon(v):
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, str):
        return v[:200]
    if isinstance(v, (list, tuple)) and v and all(isinstance(x, str) for x in v):
        return "|".join(v)[:200]
    if isinstance(v, pd.DataFrame):
        v = v.select_dtypes("number").to_numpy().ravel()
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
    ns = {"np": np, "pd": pd, "math": math, "re": re, "unicodedata": unicodedata}
    try:
        exec(src, ns)
        fn = ns[key.rsplit("::", 1)[1]]
    except Exception:
        continue
    fp = {}
    first = {"DF": frame(src), **FIRST}
    for n in range(max(nreq, 1), min(ntot, 3) + 1):
        for i, combo in enumerate(itertools.product(first.items(), *([REST.items()] * (n - 1)))):
            if i >= 150:
                break
            try:
                with np.errstate(all="ignore"):
                    v = canon(fn(*[c[1] for c in combo]))
            except Exception:
                continue
            fp["|".join(c[0] for c in combo)] = "∅" if v is None else v
    if any(v != "∅" for v in fp.values()):
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


def _close(x, y, tol: float, floor: float) -> bool:
    if isinstance(x, str) or isinstance(y, str):
        return x == y
    return all(abs(a - b) <= tol * max(floor, abs(a)) for a, b in zip(x, y))


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
            pairs = [(fps[key][k], fp[k]) for k in common]
            real = [(x, y) for x, y in pairs if x != NA and y != NA]  # entradas em que as duas respondem
            # escalar x série, texto x número: não é a mesma conta
            if any(type(x) is not type(y) or (not isinstance(x, str) and len(x) != len(y)) for x, y in real):
                continue
            # ponytail: saída igual em todas as entradas (validador que só diz False, função constante) não prova
            # nada; exige 2 saídas distintas. Texto compara por igualdade nas ~8 entradas fixas: se dois
            # tratamentos diferentes passarem por iguais, acrescente em FIRST a entrada que os separa.
            if len({str(x) for x, _ in real}) < 2:
                continue
            # uma só roda em 4 entradas (só número) e a outra em 20 (número e texto): igual no pouco em comum
            # é pista, não prova
            wide = max(sum(v != NA for v in fps[key].values()), sum(v != NA for v in fp.values()))
            if all(_close(x, y, 1e-7, 1.0) for x, y in pairs) and 2 * len(real) >= wide:
                kind = "execucao"
            elif all(_close(x, y, NEAR, 1e-12) for x, y in real):  # inclui "uma devolve vazio, a outra não"
                kind = "execucao~"
            else:
                continue
            rel, name = other.rsplit("::", 1)
            out.setdefault(tuple(key.rsplit("::", 1)), []).append(Match(kind, len(common), rel, name, line[other]))
    for ms in out.values():
        ms.sort(key=lambda m: (m.kind != "execucao", -m.score))
    return out
