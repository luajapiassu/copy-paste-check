"""Forma canônica de fórmulas matemáticas, inclusive no meio de código pandas/numpy.

Para cada função:
1. Atribuições simples (``x = <expr>``) entram num ambiente e são substituídas
   nas expressões seguintes, então ``d = a - b; return d / b`` vira ``(a - b) / b``.
2. Toda expressão aritmética "raiz" (um ``BinOp`` cujo pai não é aritmético) é
   convertida para sympy, junto com o ``return`` final.
3. A expressão é simplificada e os símbolos são renomeados de forma canônica, para
   que nomes de variáveis e ordem de parâmetros não importem.

Equivalências tratadas: ``df["close"]`` = ``df.close``; ``x.pct_change()`` =
``x / x.shift(1) - 1``; ``x.diff()`` = ``x - x.shift(1)``; ``np.log`` = ``math.log``
= ``log``; ``log(a) - log(b)`` = ``log(a / b)``; ``x ** 0.5`` = ``sqrt(x)``.

Filtro de ruído: cada operação, função conhecida ou constante "não trivial" (fora
de 0, 1, -1, 2) soma 1 de peso. O ``return`` precisa de peso 2; expressões no meio
do código precisam de 3. Assim ``i + 1`` não entra, mas ``close / close.shift() - 1``
entra.

sympy é opcional e só é importado quando aparece uma candidata (custa ~0,4 s).
"""

from __future__ import annotations

import ast
import itertools

sp = None
_SP_MISSING = False

MIN_WEIGHT_RETURN = 2
MIN_WEIGHT_INLINE = 3
MAX_SYMBOLS = 6
_TRIVIAL = {0, 1, -1, 2}
_MATH = {"log": "log", "exp": "exp", "sqrt": "sqrt", "abs": "Abs", "fabs": "Abs", "sin": "sin", "cos": "cos"}
# Métodos/funções de agregação que viram uma função simbólica opaca f(x).
_OPAQUE = {
    "sum", "mean", "std", "var", "median", "min", "max", "cumsum", "cumprod", "prod", "count",
    "nansum", "nanmean", "nanstd", "maximum", "minimum", "where", "clip", "rolling", "expanding",
    "ewm", "round", "floor", "ceil", "dot",
}


def _sympy():
    global sp, _SP_MISSING
    if sp is None and not _SP_MISSING:
        try:
            import sympy

            sp = sympy
        except ImportError:  # pragma: no cover - sympy é opcional
            _SP_MISSING = True
    return sp


class _NotFormula(Exception):
    pass


def _ref_name(node: ast.AST) -> str:
    """``df.close``, ``df["close"]`` e ``self.df.close`` viram nomes de símbolo estáveis."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_ref_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return f"{_ref_name(node.value)}.{node.slice.value}"
    raise _NotFormula


class _Converter:
    def __init__(self, env: dict[str, tuple[object, int]]) -> None:
        self.env = env
        self.weight = 0

    def __call__(self, node: ast.AST):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if node.value not in _TRIVIAL:
                self.weight += 1
            return sp.nsimplify(node.value, rational=True)
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            name = _ref_name(node)
            if isinstance(node, ast.Name) and name in self.env:
                expr, weight = self.env[name]  # variável intermediária: herda o peso da expressão
                self.weight += weight
                return expr
            return sp.Symbol(name)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            val = self(node.operand)
            return -val if isinstance(node.op, ast.USub) else val
        if isinstance(node, ast.BinOp):
            left, right = self(node.left), self(node.right)
            self.weight += 1
            match node.op:
                case ast.Add():
                    return left + right
                case ast.Sub():
                    return left - right
                case ast.Mult():
                    return left * right
                case ast.Div():
                    return left / right
                case ast.Pow():
                    return left**right
            raise _NotFormula
        if isinstance(node, ast.Call):
            return self._call(node)
        raise _NotFormula

    def _call(self, node: ast.Call):
        func = node.func
        fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if fname is None:
            raise _NotFormula
        # Método pandas: x.shift(n), x.pct_change(), x.diff(), x.mean()…
        is_method = isinstance(func, ast.Attribute) and not (
            isinstance(func.value, ast.Name) and func.value.id in ("np", "numpy", "math", "pd", "sp", "scipy")
        )
        args = [self(a) for a in node.args]
        kw = {k.arg: k.value for k in node.keywords}
        if is_method:
            obj = self(func.value)
            self.weight += 1
            periods = args[0] if args else (self(kw["periods"]) if "periods" in kw else sp.Integer(1))
            shift = sp.Function("shift")
            if fname == "shift":
                return shift(obj, periods)
            if fname == "pct_change":
                return obj / shift(obj, periods) - 1
            if fname == "diff":
                return obj - shift(obj, periods)
            if fname in _MATH and not args:
                return getattr(sp, _MATH[fname])(obj)
            if fname in _OPAQUE:
                return sp.Function(fname)(obj, *args)
            raise _NotFormula
        self.weight += 1
        if fname == "log1p" and len(args) == 1:
            return sp.log(1 + args[0])
        if fname in ("power", "pow") and len(args) == 2:
            return args[0] ** args[1]
        if fname in _MATH and len(args) == 1:
            return getattr(sp, _MATH[fname])(args[0])
        if fname in _OPAQUE and args:
            return sp.Function(fname)(*args)
        raise _NotFormula


def _canonical(expr) -> str | None:
    try:
        expr = sp.cancel(sp.expand(sp.expand_log(expr, force=True)))
    except Exception:  # sympy pode falhar em casos exóticos; nunca travar
        return None
    # Soma simples de variáveis/constantes (a + b + c) é genérica demais para ser "fórmula".
    if expr.is_Add and all(t.is_Symbol or t.is_Number for t in expr.args):
        return None
    syms = sorted(expr.free_symbols, key=str)
    if not syms or len(syms) > MAX_SYMBOLS:
        return None
    # Agrupa símbolos pelo "papel" que têm na expressão; só permuta dentro de empates.
    def role(s):
        return sp.srepr(expr.xreplace({o: (sp.Symbol("S") if o == s else sp.Symbol("T")) for o in syms}))

    groups: dict[str, list] = {}
    for s in syms:
        groups.setdefault(role(s), []).append(s)
    ordered = [groups[k] for k in sorted(groups)]
    best: str | None = None
    for combo in itertools.islice(itertools.product(*(itertools.permutations(g) for g in ordered)), 120):
        flat = [s for g in combo for s in g]
        text = sp.srepr(expr.xreplace({old: sp.Symbol(f"p{i}") for i, old in enumerate(flat)}))
        if best is None or text < best:
            best = text
    return best


def _roots(stmt: ast.stmt):
    """Expressões aritméticas "raiz" dentro de um statement (sem descer em defs aninhadas)."""
    arith = (ast.BinOp, ast.UnaryOp)
    stack: list[tuple[ast.AST, bool]] = [(stmt, False)]
    while stack:
        node, parent_arith = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and node is not stmt:
            continue
        if isinstance(node, ast.BinOp) and not parent_arith:
            yield node
        is_arith = isinstance(node, arith)
        for child in ast.iter_child_nodes(node):
            stack.append((child, is_arith))


def _has_candidate(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Filtro barato (só AST) para não importar o sympy à toa."""
    for node in ast.walk(fn):
        if isinstance(node, ast.BinOp) and (isinstance(node.left, ast.BinOp) or isinstance(node.right, ast.BinOp)
                                            or isinstance(node.left, ast.Call) or isinstance(node.right, ast.Call)):
            return True
        if isinstance(node, ast.Return) and node.value is not None and any(
                isinstance(n, ast.BinOp) for n in ast.walk(node.value)):
            return True
        if isinstance(node, ast.Call) and any(isinstance(arg, ast.BinOp) for arg in node.args):
            return True
    return False


def function_formulas(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    """Lista de (forma canônica, linha) das fórmulas da função, sem repetições."""
    if not _has_candidate(fn) or _sympy() is None:
        return []
    arg_names = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    env: dict[str, tuple[object, int]] = {}
    out: dict[str, int] = {}

    def add(node: ast.AST, min_weight: int) -> None:
        conv = _Converter(env)
        try:
            expr = conv(node)
        except (_NotFormula, TypeError, ValueError, ZeroDivisionError, RecursionError, AttributeError):
            return
        if conv.weight < min_weight:
            return
        canon = _canonical(expr)
        if canon and canon not in out:
            out[canon] = node.lineno

    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    for stmt in body:
        if isinstance(stmt, ast.Return) and stmt.value is not None:
            add(stmt.value, MIN_WEIGHT_RETURN)
        else:
            for root in _roots(stmt):
                add(root, MIN_WEIGHT_INLINE)
        # Atualiza o ambiente depois de olhar o statement (ordem de execução).
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            target = stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else getattr(stmt, "target", None)
            if isinstance(target, ast.Name) and stmt.value is not None and target.id not in arg_names:
                conv = _Converter(env)
                try:
                    env[target.id] = (conv(stmt.value), conv.weight)
                except (_NotFormula, TypeError, ValueError, ZeroDivisionError, RecursionError, AttributeError):
                    env.pop(target.id, None)
            elif isinstance(target, ast.Name):
                env.pop(target.id, None)
    return list(out.items())
