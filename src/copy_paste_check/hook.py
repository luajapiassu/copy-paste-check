"""Hook do Claude Code para Write/Edit/MultiEdit em arquivos ``.py``.

Economia de tokens: o hook não imprime nada quando não há duplicata e, quando
há, devolve no máximo ``limit`` linhas curtas.

- ``PreToolUse``: bloqueia só com evidência forte (``exact``/``struct``), com o
  motivo em poucas linhas para o agente reutilizar a função existente.
- ``PostToolUse``: avisa (sem bloquear) sobre ``formula`` e ``near``.

``CPCHECK_MODE=warn`` desliga o bloqueio (tudo vira aviso no PostToolUse).
Uma ``def`` com ``# cpcheck: ignore`` na mesma linha ou na anterior é ignorada.
"""

from __future__ import annotations

import json
from datetime import datetime
import os
import sqlite3
import sys
from pathlib import Path

from .extract import FunctionInfo, extract_functions
from .index import Index, Match, find_root

STRONG = {"exact", "struct"}
IGNORE_MARK = "cpcheck: ignore"


def _apply_edits(text: str, edits: list[dict], reverse: bool = False) -> str:
    for e in reversed(edits) if reverse else edits:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        if reverse:
            old, new = new, old
        if not old:
            continue
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def _before_after(event: str, tool: str, inp: dict, path: Path, response=None) -> tuple[str, str] | None:
    disk = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    # o Claude Code manda o conteúdo anterior no PostToolUse (None = arquivo novo): sem isso, reescrever um
    # arquivo com Write tratava todas as funções como novas e repetia os avisos
    if event == "PostToolUse" and isinstance(response, dict) and "originalFile" in response:
        return response["originalFile"] or "", disk
    edits = inp.get("edits") if tool == "MultiEdit" else [inp] if tool == "Edit" else None
    if tool == "Write":
        content = inp.get("content", "")
        return (disk, content) if event == "PreToolUse" else ("", disk)
    if edits is None:
        return None
    if event == "PreToolUse":
        return disk, _apply_edits(disk, edits)
    return _apply_edits(disk, edits, reverse=True), disk


def _ignored(source_lines: list[str], f: FunctionInfo) -> bool:
    for ln in (f.lineno - 1, f.lineno - 2):
        if 0 <= ln < len(source_lines) and IGNORE_MARK in source_lines[ln]:
            return True
    return False


def new_or_changed(before: str, after: str, rel: str, min_tokens: int, formulas: bool = True) -> list[FunctionInfo]:
    old = {(f.qualname, f.h_struct) for f in extract_functions(before, rel, min_tokens, formulas)}
    lines = after.splitlines()
    return [
        f for f in extract_functions(after, rel, min_tokens, formulas)
        if (f.qualname, f.h_struct) not in old and not _ignored(lines, f)
    ]


def check_change(event: str, tool: str, inp: dict, mode: str, limit: int = 3,
                 response=None, stats: dict | None = None) -> tuple[str, list[str]] | None:
    """Devolve (nível, linhas) — nível é ``block`` ou ``warn`` — ou None."""
    file_path = inp.get("file_path", "")
    if not file_path.endswith(".py"):
        return None
    path = Path(file_path)
    pair = _before_after(event, tool, inp, path, response)
    if pair is None:
        return None
    root = find_root(path.parent.resolve())
    idx = Index(root)
    try:
        try:
            idx.scan()
        except sqlite3.OperationalError:  # outro hook gravando no índice: consulta o que já existe
            idx.db.rollback()
        rel = idx.rel(path)
        funcs = new_or_changed(pair[0], pair[1], rel, idx.min_tokens, idx.config.formula)
        # no PreToolUse o índice ainda tem a versão antiga do arquivo: função que a edição renomeia ou
        # apaga não é "existente" (senão renomear bloqueava contra si mesma)
        stays = {f.qualname for f in extract_functions(pair[1], rel, 1, False)}
        hits: list[tuple[FunctionInfo, Match]] = []
        for f in funcs:
            hits.extend((f, m) for m in idx.query(f, limit=limit)
                        if m.path != rel or m.qualname in stays - {f.qualname})
    finally:
        idx.close()
    if stats is not None:  # alimenta o hook.log: o que foi checado, mesmo quando nada foi achado
        stats.update(root=root, arquivo=rel, checadas=len(funcs), achados=sorted({m.kind for _, m in hits}))

    strong = [(f, m) for f, m in hits if m.kind in STRONG]
    weak = [(f, m) for f, m in hits if m.kind not in STRONG]
    if event == "PreToolUse":
        chosen, level = (strong, "block") if mode == "block" else ([], "")
    else:
        chosen, level = (weak if mode == "block" else hits), "warn"
    if not chosen:
        return None
    # Uma linha por função nova: a melhor correspondência por extenso, as demais só como caminho.
    grouped: dict[str, list[Match]] = {}
    for f, m in chosen:
        grouped.setdefault(f.qualname, []).append(m)
    lines = []
    for qual, ms in list(grouped.items())[:limit]:
        best = ms[0]
        extra = f"; também em {', '.join(f'{m.path}:{m.lineno}' for m in ms[1:])}" if len(ms) > 1 else ""
        lines.append(f"`{qual}` (nova) ≈ `{best.qualname}` em {best.path}:{best.lineno} [{best.kind}]{extra}")
    return level, lines


def _log(stats: dict, event: str, mode: str, level: str) -> None:
    """Uma linha JSON por execução em ``.cpcheck/hook.log``: sem isso não dá para saber se o hook
    rodou e não achou nada ou se nem olhou (``checadas`` = 0: função pequena ou nada novo)."""
    if "root" not in stats:
        return
    rec = {"t": datetime.now().isoformat(timespec="seconds"), "evento": event, "modo": mode,
           "arquivo": stats["arquivo"], "checadas": stats["checadas"], "achados": stats["achados"], "nivel": level}
    try:
        with open(Path(stats["root"]) / ".cpcheck" / "hook.log", "a", encoding="utf-8") as f:
            print(json.dumps(rec, ensure_ascii=False), file=f)
    except OSError:  # log nunca pode travar o agente
        pass


def run(stream=sys.stdin) -> int:
    try:
        payload = json.load(stream)
    except json.JSONDecodeError:
        return 0
    event = payload.get("hook_event_name", "PreToolUse")
    tool = payload.get("tool_name", "")
    if tool not in ("Write", "Edit", "MultiEdit"):
        return 0
    mode = os.environ.get("CPCHECK_MODE", "block")
    stats: dict = {}
    try:
        result = check_change(event, tool, payload.get("tool_input") or {}, mode,
                              response=payload.get("tool_response"), stats=stats)
    except Exception as exc:  # o hook nunca pode travar o agente
        print(f"cpcheck: erro ignorado ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0
    _log(stats, event, mode, result[0] if result else "")
    if result is None:
        return 0
    level, lines = result
    body = "\n".join(f"- {line}" for line in lines)
    if level == "block":
        reason = (
            f"cpcheck: já existe função equivalente no repo:\n{body}\n"
            "Importe e use a existente em vez de recriar. Se está movendo a função, apague a original antes. "
            "Se a cópia for intencional, marque a def com `# cpcheck: ignore`."
        )
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": reason}}
    else:
        ctx = f"cpcheck: possível duplicata (avalie reutilizar a existente):\n{body}"
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ctx}}
    print(json.dumps(out, ensure_ascii=False))
    return 0
