"""Configuração por repositório: ``.cpcheck.toml`` ou ``[tool.cpcheck]`` no ``pyproject.toml``.

Exemplo::

    exclude = ["pocs/*", "scripts/_backfill_*.py"]
    min_tokens = 40
    formula = false   # true liga a camada de fórmulas (sympy)
    near = true       # MinHash (cópias com algumas linhas alteradas); false desliga
    near_threshold = 0.75
    formula_max_funcs = 5
    # Duplicações intencionais: "glob do caminho::glob do nome da função".
    # Essas funções não são cobradas por serem cópia, mas continuam aparecendo
    # como origem a reutilizar quando alguém cria uma cópia nova em outro lugar.
    allow = ["templates/*/*.py::_parse_number"]
"""

from __future__ import annotations

import fnmatch
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def is_test_path(path: str) -> bool:
    """Arquivo de teste pelo padrão do pytest (``backtest.py`` e ``latest.py`` não são teste)."""
    *dirs, name = path.split("/")
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or "tests" in dirs


@dataclass(slots=True)
class Config:
    exclude: list[str] = field(default_factory=list)
    min_tokens: int = 40
    formula: bool = False  # exige sympy (pip install ".[formula]")
    # Ligado por padrão: custa ~2 s a mais no primeiro scan e ~1 ms por função consultada (medido em ~850
    # arquivos) e só avisa, nunca bloqueia. É a camada que pega cópia com linhas alteradas.
    near: bool = True
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
        unknown = sorted(set(data) - set(cls.__dataclass_fields__))
        if unknown:  # erro de digitação (near_treshold) não pode passar calado
            print(f"cpcheck: chave desconhecida na configuração, ignorada: {', '.join(unknown)}", file=sys.stderr)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
