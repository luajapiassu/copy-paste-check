"""Mede o ``review`` num repositório com um gabarito seu: ``cpcheck bench gabarito.toml``.

O gabarito mora no repositório analisado (ou fora dele), nunca aqui: cada caso é uma função "nova" e o
nome da(s) função(ões) existente(s) que ela duplica. Lista vazia = não existe nada igual::

    [[caso]]
    espera = ["vol_trailing"]
    codigo = '''
    def _vol_anual(precos):
        r = precos.pct_change().dropna()
        return float(r.std() * 252 ** 0.5)
    '''

Os casos são plantados em ``.cpcheck/bench/`` (ignorado pelo git e pelo scan) e apagados no fim.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .extract import extract_functions
from .index import Index
from .judge import Runner, review, run_claude


def load(path: Path) -> list[dict]:
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))["caso"]


def run(idx: Index, cases: list[dict], runner: Runner = run_claude, execute: bool = True) -> list[dict]:
    """Uma linha por caso: ``{nome, espera, achou, ok, origem, ressalva}``."""
    folder = idx.root / ".cpcheck" / "bench"
    folder.mkdir(exist_ok=True)
    changed = []
    try:
        for i, case in enumerate(cases):
            rel = f".cpcheck/bench/caso_{i}.py"
            (idx.root / rel).write_text(case["codigo"], encoding="utf-8")
            changed.append((rel, extract_functions(case["codigo"], rel, 1, False)[:1]))
        found = {f.path: f for f in review(idx, changed, runner=runner, execute=execute, expand=True)}
    finally:
        for file in folder.glob("caso_*.py"):
            file.unlink()
    rows = []
    for (rel, funcs), case in zip(changed, cases):
        f = found.get(rel)
        got = f.canonica.qualname.rsplit(".", 1)[-1] if f and f.canonica else ""
        rows.append({"nome": funcs[0].qualname if funcs else "?", "espera": case.get("espera", []), "achou": got,
                     "ok": got in case["espera"] if case.get("espera") else not got,
                     "origem": f.origem if f else "sem candidata", "ressalva": f.ressalva if f else ""})
    return rows
