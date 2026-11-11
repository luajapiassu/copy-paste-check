"""Configuração por repositório: ``.cpcheck.toml`` ou ``[tool.cpcheck]`` no ``pyproject.toml``.

Exemplo::

    exclude = ["pocs/*", "scripts/_backfill_*.py"]
    min_tokens = 40
    formula = false   # true liga a camada de fórmulas (sympy)
    near = false      # true liga o MinHash (cópias aproximadas)
    near_threshold = 0.75
    formula_max_funcs = 5
    # Duplicações intencionais: "glob do caminho::glob do nome da função".
    # Essas funções não são cobradas por serem cópia, mas continuam aparecendo
    # como origem a reutilizar quando alguém cria uma cópia nova em outro lugar.
    allow = ["shared/extratos/templates_*/*.py::_parse_br_number"]
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Config:
    exclude: list[str] = field(default_factory=list)
    min_tokens: int = 40
    # Camadas caras, desligadas por padrão: exact/struct são sempre ligadas.
    formula: bool = False  # exige sympy (pip install ".[formula]")
    near: bool = False
    near_threshold: float = 0.75
    # Fórmula que aparece em mais funções que isso é idioma, não duplicata.
    formula_max_funcs: int = 5
    allow: list[str] = field(default_factory=list)
    # Em testes é normal repetir a conta do código testado; não indexa fórmulas ali.
    formula_exclude: list[str] = field(
        default_factory=lambda: ["tests/*", "*/tests/*", "test_*.py", "*/test_*.py", "*_test.py"]
    )

    def formulas_enabled(self, path: str) -> bool:
        return not any(fnmatch.fnmatch(path, pat) for pat in self.formula_exclude)

    def allowed(self, path: str, qualname: str) -> bool:
        for rule in self.allow:
            path_glob, _, name_glob = rule.partition("::")
            if fnmatch.fnmatch(path, path_glob) and fnmatch.fnmatch(qualname, name_glob or "*"):
                return True
        return False

    @classmethod
    def load(cls, root: Path) -> Config:
        data: dict = {}
        own = root / ".cpcheck.toml"
        if own.exists():
            data = tomllib.loads(own.read_text(encoding="utf-8"))
            data = data.get("cpcheck", data)
        elif (root / "pyproject.toml").exists():
            try:
                data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
                data = data.get("tool", {}).get("cpcheck", {})
            except tomllib.TOMLDecodeError:
                data = {}
        known = {k: data[k] for k in ("exclude", "min_tokens", "formula", "near", "near_threshold", "formula_max_funcs", "allow", "formula_exclude") if k in data}
        return cls(**known)
