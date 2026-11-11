import sys
import textwrap
from pathlib import Path

import pytest

from copy_paste_check.config import Config
from copy_paste_check.extract import extract_functions
from copy_paste_check.index import Index

from labeled_near import CASES

# Fundo comum: o idf só faz sentido com um repo de verdade ao redor.
BACKGROUND = [
    f"def helper_{i}(a, b):\n    total = 0\n    for x in range({i + 3}):\n        total += a * x + b\n"
    f"    if total > {i * 7}:\n        return total\n    return None\n" for i in range(30)
]


def _first(src: str, path: str):
    return extract_functions(textwrap.dedent(src), path, min_tokens=1)[0]


def _score(tmp_path: Path, a: str, b: str) -> bool:
    (tmp_path / ".git").mkdir()
    (tmp_path / "base.py").write_text("\n\n".join(BACKGROUND) + "\n\n" + textwrap.dedent(b))
    idx = Index(tmp_path, Config(near=True, min_tokens=1))
    try:
        idx.scan()
        return any(m.kind == "near" for m in idx.query(_first(a, "novo.py"), limit=5))
    finally:
        idx.close()


@pytest.mark.parametrize("label,name,a,b", [(c[0], c[1], c[2], c[3]) for c in CASES], ids=[c[1] for c in CASES])
def test_near_labels(tmp_path, label, name, a, b):
    if label == "real_dificil":
        pytest.xfail("limite conhecido do near: pares curtos com cláusula extra ficam abaixo do ruído")
    assert _score(tmp_path, a, b) == (label == "real"), name
