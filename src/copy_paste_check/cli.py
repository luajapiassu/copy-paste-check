"""CLI ``cpcheck``: scan, check, diff e hook."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import hook
from . import judge as judge_mod
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


def _candidates(idx: Index, a) -> list:
    """Funções a julgar: as dos arquivos pedidos ou, sem arquivos, as novas/alteradas vs --base."""
    results = []
    if a.files:
        files = [idx.rel(f) for f in a.files]
    else:
        files = [p for p in _git(idx.root, "diff", "--name-only", "--diff-filter=AMR", f"{a.base}...HEAD").split()
                 if p.endswith(".py")]
    for rel in files:
        path = idx.root / rel
        if not path.exists():
            continue
        after = path.read_text(encoding="utf-8", errors="replace")
        if a.files:
            funcs = extract_functions(after, rel, idx.min_tokens, idx.config.formula)
        else:
            try:
                before = _git(idx.root, "show", f"{a.base}:{rel}")
            except subprocess.CalledProcessError:
                before = ""
            funcs = hook.new_or_changed(before, after, rel, idx.min_tokens)
        for f in funcs:
            ms = idx.query(f, limit=a.limit)
            if ms:
                results.append((rel, f.qualname, f.lineno, ms))
    return results


def cmd_judge(a) -> int:
    idx = _open(a.root)
    idx.scan()
    verdicts = judge_mod.judge(idx, _candidates(idx, a), low=a.low, high=a.high, remember=not a.no_remember)
    idx.close()
    for v in verdicts:
        print(f"{v.path}:{v.lineno} {v.qualname} ~ {v.other.path}:{v.other.lineno} {v.other.qualname}"
              f" {v.other.kind}={v.other.score:.2f} [{v.veredito}/{v.origem}] -> {v.canonica} | {v.diferencas}")
    return 1 if a.fail_on == "strong" and any(v.veredito == "duplicata" for v in verdicts) else 0


def cmd_ask(a) -> int:
    print(judge_mod.ask(Path(a.root) if a.root else find_root(Path.cwd()), a.items))
    return 0


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

    j = sub.add_parser("judge", help="Haiku julga os candidatos ambíguos (near 0,70-0,85, formula); sem arquivos: o diff vs --base")
    j.add_argument("files", nargs="*")
    j.add_argument("--base", default="origin/main")
    j.add_argument("--limit", type=int, default=3)
    j.add_argument("--low", type=float, default=0.70)
    j.add_argument("--high", type=float, default=0.95)
    j.add_argument("--no-remember", action="store_true", help="não grava em cpcheck-verdicts.json")
    j.add_argument("--fail-on", choices=["strong", "none"], default="none")
    j.set_defaults(fn=cmd_judge)

    k = sub.add_parser("ask", help="busca prévia em linguagem natural (Haiku, só leitura)")
    k.add_argument("items", nargs="+", help="o que você vai precisar, ex.: 'lista de dias úteis entre datas'")
    k.set_defaults(fn=cmd_ask)

    h = sub.add_parser("hook", help="hook do Claude Code (lê JSON do stdin)")
    h.set_defaults(fn=lambda a: hook.run())

    for stream in (sys.stdout, sys.stderr):  # Windows cp1252 não imprime setas nem acentos do modelo
        stream.reconfigure(encoding="utf-8", errors="replace")
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
