"""CLI ``cpcheck``: scan, check, diff e hook."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import hook
from .extract import extract_functions
from .index import Index, find_root


def _open(root: str | None) -> Index:
    return Index(Path(root) if root else find_root(Path.cwd()))


def cmd_scan(a) -> int:
    idx = _open(a.root)
    changed, removed = idx.scan(a.exclude)
    total = idx.db.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
    print(f"cpcheck: {changed} arquivos indexados, {removed} removidos, {total} funções no índice")
    idx.close()
    return 0


def _report(results: list[tuple[str, str, int, list]], as_json: bool, fail_on: str) -> int:
    """Imprime em formato compacto e devolve o exit code conforme ``fail_on``."""
    if as_json:
        print(json.dumps([
            {"path": p, "function": q, "line": ln, "matches": [asdict(m) for m in ms]}
            for p, q, ln, ms in results
        ], ensure_ascii=False))
    else:
        for p, q, ln, ms in results:
            for m in ms:
                print(f"{p}:{ln} {q} ~ {m.short()}")
    kinds = {m.kind for *_, ms in results for m in ms}
    if fail_on == "any" and kinds:
        return 1
    if fail_on == "strong" and kinds & hook.STRONG:
        return 1
    return 0


def cmd_check(a) -> int:
    idx = _open(a.root)
    idx.scan()
    results = []
    for file in a.files:
        rel = idx.rel(file)
        source = Path(file).read_text(encoding="utf-8", errors="replace")
        for f in extract_functions(source, rel, idx.min_tokens, idx.config.formula):
            ms = idx.query(f, limit=a.limit)
            if ms:
                results.append((rel, f.qualname, f.lineno, ms))
    idx.close()
    return _report(results, a.json, a.fail_on)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def cmd_diff(a) -> int:
    """Checa só funções novas/alteradas em relação a ``--base`` (uso em PR)."""
    idx = _open(a.root)
    idx.scan()
    files = [p for p in _git(idx.root, "diff", "--name-only", "--diff-filter=AMR", f"{a.base}...HEAD").split()
             if p.endswith(".py")]
    results = []
    for rel in files:
        path = idx.root / rel
        if not path.exists():
            continue
        try:
            before = _git(idx.root, "show", f"{a.base}:{rel}")
        except subprocess.CalledProcessError:
            before = ""
        after = path.read_text(encoding="utf-8", errors="replace")
        for f in hook.new_or_changed(before, after, rel, idx.min_tokens):
            ms = idx.query(f, limit=a.limit)
            if ms:
                results.append((rel, f.qualname, f.lineno, ms))
    idx.close()
    if a.markdown:
        if results:
            print("### cpcheck: possíveis duplicatas\n")
            for p, q, ln, ms in results:
                for m in ms:
                    print(f"- `{p}:{ln}` `{q}` ~ **{m.kind}** {m.score:.2f} `{m.path}:{m.lineno}` `{m.qualname}`")
        kinds = {m.kind for *_, ms in results for m in ms}
        return 1 if (a.fail_on == "any" and kinds) or (a.fail_on == "strong" and kinds & hook.STRONG) else 0
    return _report(results, a.json, a.fail_on)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cpcheck", description="Detecta funções e fórmulas duplicadas.")
    p.add_argument("--root", help="raiz do repositório (padrão: procura .git a partir do cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="atualiza o índice (incremental)")
    s.add_argument("--exclude", action="append", default=[], help="glob a ignorar (repetível)")
    s.set_defaults(fn=cmd_scan)

    for name, fn, help_ in (("check", cmd_check, "checa arquivos contra o índice"),
                            ("diff", cmd_diff, "checa só o que mudou desde --base")):
        c = sub.add_parser(name, help=help_)
        if name == "check":
            c.add_argument("files", nargs="+")
        else:
            c.add_argument("--base", default="origin/main")
            c.add_argument("--markdown", action="store_true", help="saída para comentário de PR")
        c.add_argument("--json", action="store_true")
        c.add_argument("--limit", type=int, default=3)
        c.add_argument("--fail-on", choices=["strong", "any", "none"], default="strong")
        c.set_defaults(fn=fn)

    h = sub.add_parser("hook", help="hook do Claude Code (lê JSON do stdin)")
    h.set_defaults(fn=lambda a: hook.run())

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
