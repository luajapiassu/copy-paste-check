"""Extrai funções de código Python e calcula as impressões digitais de cada uma.

Três níveis, do mais estrito ao mais solto:
- ``h_exact``: corpo idêntico (ignora nome da função, docstring e comentários).
- ``h_struct``: mesma estrutura com variáveis locais/argumentos renomeados e
  literais trocados pelo tipo (clone "Tipo 2").
- ``tokens``: sequência normalizada usada pelo MinHash para clones aproximados.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field

from .formula import function_formulas


@dataclass(slots=True)
class FunctionInfo:
    path: str
    qualname: str
    lineno: int
    end_lineno: int
    n_tokens: int
    h_exact: str
    h_struct: str
    tokens: list[str] = field(repr=False)
    formulas: list[tuple[str, int]] = field(default_factory=list)  # (forma canônica, linha)


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        if isinstance(body[0].value.value, str):
            return body[1:]
    return body


def _local_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


_SKIP = (ast.expr_context, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)


def _serialize(fn: ast.FunctionDef | ast.AsyncFunctionDef, local: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Uma passada em pré-ordem (ordem do código), sem copiar a árvore.

    Devolve ``(exato, estrutura, tokens)``: ``exato`` mantém nomes e literais
    (vira o ``h_exact``); a estrutura tem marcadores de aninhamento e vira o
    ``h_struct``; os tokens alimentam o MinHash. Locais viram v0, v1… na
    ordem em que aparecem e literais viram o nome do tipo. A pré-ordem faz uma
    linha a mais mexer só nos shingles vizinhos (com BFS a mudança se espalharia).
    """
    mapping: dict[str, str] = {}
    exact: list[str] = []
    struct: list[str] = []
    toks: list[str] = []

    def name(n: str) -> str:
        return mapping.setdefault(n, f"v{len(mapping)}") if n in local else n

    def visit(node: ast.AST) -> None:
        kind = type(node).__name__
        toks.append(kind)
        struct.append(kind)
        exact.append(kind)
        if isinstance(node, ast.Name):
            exact.append(node.id)
            toks.append(name(node.id))
            struct.append(toks[-1])
        elif isinstance(node, ast.arg):
            exact.append(node.arg)
            struct.append(name(node.arg))
            return  # anotações de tipo não contam
        elif isinstance(node, ast.Constant):
            exact.append(repr(node.value))
            struct.append(type(node.value).__name__)
        elif isinstance(node, ast.Attribute):
            toks.append("." + node.attr)
            struct.append(toks[-1])
            exact.append(toks[-1])
        elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.AugAssign)):
            toks.append(type(node.op).__name__)
            struct.append(toks[-1])
            exact.append(toks[-1])
        elif isinstance(node, ast.Compare):
            toks.extend(type(o).__name__ for o in node.ops)
            struct.extend(toks[-len(node.ops):])
            exact.extend(toks[-len(node.ops):])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exact.append(node.name)
        elif isinstance(node, ast.alias):
            exact.append(f"{node.name}>{node.asname}")
        elif isinstance(node, ast.keyword):
            exact.append(str(node.arg))
            struct.append(str(node.arg))
        struct.append("(")
        exact.append("(")
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _SKIP):
                visit(child)
        struct.append(")")
        exact.append(")")

    visit(fn.args)
    for stmt in _strip_docstring(fn.body):
        visit(stmt)
    return exact, struct, toks


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=12).hexdigest()


def fingerprint(fn: ast.FunctionDef | ast.AsyncFunctionDef, path: str, qualname: str, formulas: bool = True) -> FunctionInfo:
    exact, struct, toks = _serialize(fn, _local_names(fn))
    return FunctionInfo(
        path=path,
        qualname=qualname,
        lineno=fn.lineno,
        end_lineno=getattr(fn, "end_lineno", fn.lineno) or fn.lineno,
        n_tokens=len(toks),
        h_exact=_digest(" ".join(exact)),
        h_struct=_digest(" ".join(struct)),
        tokens=toks,
        formulas=function_formulas(fn) if formulas else [],
    )


def extract_functions(source: str, path: str, min_tokens: int = 40, formulas: bool = True) -> list[FunctionInfo]:
    """Devolve as funções do código que têm pelo menos ``min_tokens`` tokens.

    Fórmulas são mantidas mesmo abaixo do mínimo, porque costumam ser curtas.
    Código que não compila devolve lista vazia (o hook nunca deve travar o agente).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    found: list[FunctionInfo] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                info = fingerprint(child, path, qual, formulas)
                if info.n_tokens >= min_tokens or info.formulas:
                    found.append(info)
                visit(child, qual + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(tree, "")
    return found
