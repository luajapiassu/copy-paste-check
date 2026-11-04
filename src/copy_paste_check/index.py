"""Índice persistente de funções em SQLite (``.cpcheck/index.db`` na raiz do repo).

A atualização é incremental: só reprocessa arquivos cujo hash mudou.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import minhash
from .config import Config
from .extract import FunctionInfo, extract_functions

DEFAULT_EXCLUDE = [
    ".git/*", "*/.git/*", ".venv/*", "*/.venv/*", "venv/*", "node_modules/*", "*/node_modules/*",
    "*/__pycache__/*", "build/*", "dist/*", "*/alembic/versions/*", ".cpcheck/*",
]
SCHEMA_VERSION = 2  # mudou o formato? o índice é recriado sozinho
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha TEXT NOT NULL, stamp TEXT);
CREATE TABLE IF NOT EXISTS functions (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, qualname TEXT NOT NULL,
    lineno INTEGER, end_lineno INTEGER, n_tokens INTEGER,
    h_exact TEXT, h_struct TEXT, sig BLOB
);
CREATE TABLE IF NOT EXISTS formulas (canon TEXT NOT NULL, fn_id INTEGER NOT NULL, lineno INTEGER);
CREATE INDEX IF NOT EXISTS ix_formula ON formulas(canon);
CREATE INDEX IF NOT EXISTS ix_formula_fn ON formulas(fn_id);
CREATE INDEX IF NOT EXISTS ix_fn_path ON functions(path);
CREATE INDEX IF NOT EXISTS ix_fn_exact ON functions(h_exact);
CREATE INDEX IF NOT EXISTS ix_fn_struct ON functions(h_struct);
CREATE TABLE IF NOT EXISTS bands (band TEXT NOT NULL, fn_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_band ON bands(band);
CREATE INDEX IF NOT EXISTS ix_band_fn ON bands(fn_id);
"""


@dataclass(slots=True, frozen=True)
class Match:
    kind: str  # exact | struct | formula | near
    score: float
    path: str
    qualname: str
    lineno: int

    def short(self) -> str:
        return f"{self.kind} {self.score:.2f} {self.path}:{self.lineno} {self.qualname}"


def _sha(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / ".git").exists() or (p / ".cpcheck").exists():
            return p
    return start


class Index:
    def __init__(self, root: Path, config: Config | None = None) -> None:
        self.root = root.resolve()
        self.config = config or Config.load(self.root)
        self.min_tokens = self.config.min_tokens
        db_dir = self.root / ".cpcheck"
        db_dir.mkdir(exist_ok=True)
        db_path = db_dir / "index.db"
        self.db = sqlite3.connect(db_path)
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self.db.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{db_path}{suffix}").unlink(missing_ok=True)
            self.db = sqlite3.connect(db_path)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def rel(self, path: Path | str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    # ---- escrita -----------------------------------------------------------
    def _iter_py(self, exclude: list[str]):
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            dirnames[:] = [
                d for d in dirnames
                if not any(fnmatch.fnmatch(f"{rel_dir}/{d}/x".lstrip("./"), pat) for pat in exclude)
            ]
            for name in filenames:
                if name.endswith(".py"):
                    rel = f"{rel_dir}/{name}".removeprefix("./")
                    if not any(fnmatch.fnmatch(rel, pat) for pat in exclude):
                        yield rel

    def _delete(self, rel: str) -> None:
        self.db.execute("DELETE FROM bands WHERE fn_id IN (SELECT id FROM functions WHERE path=?)", (rel,))
        self.db.execute("DELETE FROM formulas WHERE fn_id IN (SELECT id FROM functions WHERE path=?)", (rel,))
        self.db.execute("DELETE FROM functions WHERE path=?", (rel,))
        self.db.execute("DELETE FROM files WHERE path=?", (rel,))

    def _insert(self, rel: str, sha: str, stamp: str, funcs: list[FunctionInfo]) -> None:
        self.db.execute("INSERT OR REPLACE INTO files(path, sha, stamp) VALUES (?, ?, ?)", (rel, sha, stamp))
        for f in funcs:
            sig = minhash.signature(f.tokens)
            cur = self.db.execute(
                "INSERT INTO functions(path, qualname, lineno, end_lineno, n_tokens, h_exact, h_struct, sig)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rel, f.qualname, f.lineno, f.end_lineno, f.n_tokens, f.h_exact, f.h_struct, minhash.to_bytes(sig)),
            )
            self.db.executemany(
                "INSERT INTO formulas(canon, fn_id, lineno) VALUES (?, ?, ?)",
                [(c, cur.lastrowid, ln) for c, ln in f.formulas] if self.config.formulas_enabled(rel) else [],
            )
            self.db.executemany(
                "INSERT INTO bands(band, fn_id) VALUES (?, ?)", [(b, cur.lastrowid) for b in minhash.bands(sig)]
            )

    def scan(self, exclude: list[str] | None = None) -> tuple[int, int]:
        """Atualiza o índice. Devolve (arquivos reprocessados, arquivos removidos)."""
        exclude = DEFAULT_EXCLUDE + self.config.exclude + (exclude or [])
        known = {p: (sha, stamp) for p, sha, stamp in self.db.execute("SELECT path, sha, stamp FROM files")}
        seen: set[str] = set()
        changed = 0
        for rel in self._iter_py(exclude):
            seen.add(rel)
            path = self.root / rel
            st = path.stat()
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
            old_sha, old_stamp = known.get(rel, (None, None))
            if old_stamp == stamp:  # caminho rápido: nem lê o arquivo
                continue
            data = path.read_bytes()
            sha = _sha(data)
            if old_sha == sha:
                self.db.execute("UPDATE files SET stamp=? WHERE path=?", (stamp, rel))
                continue
            self._delete(rel)
            source = data.decode("utf-8", errors="replace")
            self._insert(rel, sha, stamp, extract_functions(source, rel, self.min_tokens))
            changed += 1
        removed = [p for p in known if p not in seen]
        for rel in removed:
            self._delete(rel)
        self.db.commit()
        return changed, len(removed)

    # ---- leitura -----------------------------------------------------------
    def query(self, f: FunctionInfo, limit: int = 3) -> list[Match]:
        """Procura duplicatas de ``f`` no índice, ignorando a própria função.

        Funções em ``allow`` (duplicação intencional) não são cobradas, mas
        continuam valendo como origem a reutilizar para quem está fora da regra.
        """
        if self.config.allowed(f.path, f.qualname):
            return []
        near_threshold = self.config.near_threshold
        own = (f.path, f.qualname)
        found: dict[tuple[str, str], Match] = {}

        def add(kind: str, score: float, rows) -> None:
            for path, qual, line in rows:
                key = (path, qual)
                if key != own and key not in found:
                    found[key] = Match(kind, score, path, qual, line)

        add("exact", 1.0, self.db.execute("SELECT path, qualname, lineno FROM functions WHERE h_exact=?", (f.h_exact,)))
        add("struct", 1.0, self.db.execute("SELECT path, qualname, lineno FROM functions WHERE h_struct=?", (f.h_struct,)))
        for canon, _ in (f.formulas if self.config.formulas_enabled(f.path) else []):
            add("formula", 1.0, self.db.execute(
                "SELECT fn.path, fn.qualname, fo.lineno FROM formulas fo JOIN functions fn ON fn.id=fo.fn_id"
                " WHERE fo.canon=?", (canon,)))

        if f.n_tokens >= self.min_tokens and len(found) < limit:
            sig = minhash.signature(f.tokens)
            band_list = minhash.bands(sig)
            marks = ",".join("?" * len(band_list))
            rows = self.db.execute(
                f"SELECT DISTINCT fn.path, fn.qualname, fn.lineno, fn.sig FROM bands b JOIN functions fn ON fn.id=b.fn_id"
                f" WHERE b.band IN ({marks})",
                band_list,
            ).fetchall()
            near = []
            for path, qual, line, blob in rows:
                score = minhash.similarity(sig, minhash.from_bytes(blob))
                if score >= near_threshold:
                    near.append((score, path, qual, line))
            for score, path, qual, line in sorted(near, reverse=True):
                add("near", score, [(path, qual, line)])

        order = {"exact": 0, "struct": 1, "formula": 2, "near": 3}
        return sorted(found.values(), key=lambda m: (order[m.kind], -m.score))[:limit]
