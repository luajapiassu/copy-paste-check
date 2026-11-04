"""Forma canônica de fórmulas matemáticas.

Pega funções cujo corpo é só atribuições + ``return <expressão aritmética>``,
substitui as variáveis intermediárias, simplifica com sympy e devolve uma string
canônica. Assim ``(a - b) / b`` e ``a / b - 1`` viram a mesma coisa.

Os argumentos são renomeados por posição (x0, x1…) e testamos as permutações
(até 4 argumentos) para que a ordem dos parâmetros não importe.
sympy é opcional: sem ele, a camada de fórmulas fica desligada.
"""

from __future__ import annotations

import ast
import itertools

sp = None  # carregado sob demanda: importar sympy custa ~0,4 s e o hook precisa ser rápido
_SP_MISSING = False


def _sympy():
    global sp, _SP_MISSING
    if sp is None and not _SP_MISSING:
        try:
            import sympy

            sp = sympy
        except ImportError:  # pragma: no cover - sympy é opcional
            _SP_MISSING = True
    return sp


def _looks_like_formula(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Filtro barato (só AST) antes de tocar no sympy."""
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    if not body or len(body) > 8 or not isinstance(body[-1], ast.Return) or body[-1].value is None:
        return False
    if not all(isinstance(s, (ast.Assign, ast.AnnAssign)) for s in body[:-1]):
        return False
    return any(isinstance(n, ast.BinOp) for s in body for n in ast.walk(s))

_FUNCS = {"log": "log", "exp": "exp", "sqrt": "sqrt", "abs": "Abs", "fabs": "Abs", "log1p": None, "power": None}
_MIN_OPS = 2
_MAX_PERM_ARGS = 4


class _NotFormula(Exception):
    pass


def _attr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attr_name(node.value)}_{node.attr}"
    raise _NotFormula


def _to_sympy(node: ast.AST, env: dict[str, object], ops: list[int]):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return sp.nsimplify(node.value, rational=True)
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        return sp.Symbol(node.id)
    if isinstance(node, ast.Attribute):
        return sp.Symbol(_attr_name(node))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        val = _to_sympy(node.operand, env, ops)
        return -val if isinstance(node.op, ast.USub) else val
    if isinstance(node, ast.BinOp):
        left, right = _to_sympy(node.left, env, ops), _to_sympy(node.right, env, ops)
        ops[0] += 1
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
    if isinstance(node, ast.Call) and not node.keywords:
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        args = [_to_sympy(a, env, ops) for a in node.args]
        ops[0] += 1
        if fname == "log1p" and len(args) == 1:
            return sp.log(1 + args[0])
        if fname in ("power", "pow") and len(args) == 2:
            return args[0] ** args[1]
        target = _FUNCS.get(fname or "")
        if target and len(args) == 1:
            return getattr(sp, target)(args[0])
    raise _NotFormula


def _body_expr(fn: ast.FunctionDef | ast.AsyncFunctionDef, env: dict[str, object], ops: list[int]):
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    if not body or len(body) > 8:
        raise _NotFormula
    for stmt in body[:-1]:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            env[stmt.targets[0].id] = _to_sympy(stmt.value, env, ops)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            env[stmt.target.id] = _to_sympy(stmt.value, env, ops)
        else:
            raise _NotFormula
    last = body[-1]
    if not isinstance(last, ast.Return) or last.value is None:
        raise _NotFormula
    return _to_sympy(last.value, env, ops)


def canonical_formula(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    if not _looks_like_formula(fn) or _sympy() is None:
        return None
    arg_names = [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs if a.arg not in ("self", "cls")]
    placeholders = [sp.Symbol(f"x{i}") for i in range(len(arg_names))]
    ops = [0]
    try:
        expr = _body_expr(fn, dict(zip(arg_names, placeholders)), ops)
    except (_NotFormula, TypeError, ValueError, ZeroDivisionError, RecursionError):
        return None
    if ops[0] < _MIN_OPS or not expr.free_symbols:
        return None
    try:
        expr = sp.cancel(sp.expand(sp.expand_log(expr, force=True)))
    except Exception:  # sympy pode falhar em casos exóticos; nunca travar
        return None

    used = [s for s in placeholders if s in expr.free_symbols]
    if len(used) > _MAX_PERM_ARGS:
        return sp.srepr(expr)
    best: str | None = None
    for perm in itertools.permutations(used):
        renamed = expr.xreplace({old: sp.Symbol(f"p{i}") for i, old in enumerate(perm)})
        text = sp.srepr(renamed)
        if best is None or text < best:
            best = text
    return best
