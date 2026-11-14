"""CLI ``cpcheck``: scan, check, diff e hook."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from fnmatch import fnmatch
from pathlib import Path

from . import hook
from . import judge as judge_mod
from .extract import extract_functions
from .index import DEFAULT_EXCLUDE, Index, find_root


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
    # utf-8 explícito: no Windows o padrão cp1252 quebra com acento e o stdout volta None
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True,
                          encoding="utf-8", errors="replace").stdout


def _changed_funcs(idx: Index, base: str, min_tokens: int | None = None):
    """Funções novas/alteradas da working tree (commits + não commitado + não rastreado) vs merge-base com ``base``."""
    for rel, before, after in _changed_sources(idx, base):
        yield rel, hook.new_or_changed(before, after, rel, min_tokens or idx.min_tokens)


def _changed_sources(idx: Index, base: str):
    """(arquivo, código no merge-base, código na working tree) de cada .py mexido."""
    mb = _git(idx.root, "merge-base", base, "HEAD").strip()
    files = _git(idx.root, "diff", "--name-only", "--diff-filter=AMR", mb).splitlines()
    files += _git(idx.root, "ls-files", "--others", "--exclude-standard").splitlines()
    skip = DEFAULT_EXCLUDE + idx.config.exclude  # migration, venv etc. também não contam no diff
    for rel in dict.fromkeys(p for p in files if p.endswith(".py") and not any(fnmatch(p, s) for s in skip)):
        path = idx.root / rel
        if not path.exists():
            continue
        try:
            before = _git(idx.root, "show", f"{mb}:{rel}")
        except subprocess.CalledProcessError:
            before = ""
        yield rel, before, path.read_text(encoding="utf-8", errors="replace")


def _query_all(idx: Index, changed, limit: int) -> list:
    return [(rel, f.qualname, f.lineno, ms) for rel, funcs in changed for f in funcs
            if (ms := idx.query(f, limit=limit))]


def cmd_diff(a) -> int:
    """Checa só funções novas/alteradas em relação a ``--base`` (uso em PR)."""
    idx = _open(a.root)
    idx.scan()
    results = _query_all(idx, _changed_funcs(idx, a.base), a.limit)
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
    if not a.files:
        return _query_all(idx, _changed_funcs(idx, a.base), a.limit)
    rels = [idx.rel(f) for f in a.files]
    changed = [(rel, extract_functions((idx.root / rel).read_text(encoding="utf-8", errors="replace"),
                                       rel, idx.min_tokens, idx.config.formula))
               for rel in rels if (idx.root / rel).exists()]
    return _query_all(idx, changed, a.limit)


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


def review_input(idx: Index, base: str):
    """Funções mexidas no PR e quais delas são novas (não existiam no merge-base)."""
    changed, novas = [], set()
    for rel, before, after in _changed_sources(idx, base):
        funcs = hook.new_or_changed(before, after, rel, judge_mod.REVIEW_MIN_TOKENS)
        old = {f.qualname for f in extract_functions(before, rel, 1, False)}
        novas |= {(rel, f.qualname) for f in funcs if f.qualname not in old}
        changed.append((rel, funcs))
    return changed, novas


def cmd_review(a) -> int:
    """Uma passada por PR: execução das contas puras + uma chamada ao Haiku com as candidatas locais."""
    idx = _open(a.root)
    idx.scan()
    changed, novas = review_input(idx, a.base)
    found = judge_mod.review(idx, changed, execute=not a.no_exec, novas=novas)
    idx.close()
    for f in found:
        where = f"{f.canonica.path}:{f.canonica.lineno} {f.canonica.qualname}" if f.canonica else "NAO EXISTE"
        print(f"{f.path}:{f.lineno} {f.qualname} -> {where} [{f.origem}] {f.ressalva}")
    for u in judge_mod.USAGE:
        total = u["input"] + u["cache_write"] + u["cache_read"] + u["output"]
        print(f"tokens: {total} (entrada {u['input']}, cache escrita {u['cache_write']}, cache leitura "
              f"{u['cache_read']}, saída {u['output']}) | {u['ms'] / 1000:.1f} s no modelo", file=sys.stderr)
    return 1 if a.fail_on == "found" and any(f.canonica for f in found) else 0


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

    r = sub.add_parser("review", help="PR: execução das contas + 1 chamada ao Haiku com candidatas locais (vs --base)")
    r.add_argument("--base", default="origin/main")
    r.add_argument("--no-exec", action="store_true", help="não executa as funções de conta puras")
    r.add_argument("--fail-on", choices=["found", "none"], default="none")
    r.set_defaults(fn=cmd_review)

    h = sub.add_parser("hook", help="hook do Claude Code (lê JSON do stdin)")
    h.set_defaults(fn=lambda a: hook.run())

    for stream in (sys.stdout, sys.stderr):  # Windows cp1252 não imprime setas nem acentos do modelo
        stream.reconfigure(encoding="utf-8", errors="replace")
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
