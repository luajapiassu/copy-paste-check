"""Índice persistente de funções em SQLite (``.cpcheck/index.db`` na raiz do repo).

A atualização é incremental: só reprocessa arquivos cujo hash mudou.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import minhash
from .config import Config
from .extract import FunctionInfo, extract_functions

DEFAULT_EXCLUDE = [
    ".git/*", "*/.git/*", ".venv/*", "*/.venv/*", "venv/*", "node_modules/*", "*/node_modules/*",
    "*/__pycache__/*", "build/*", "dist/*", "*/alembic/versions/*", ".cpcheck/*",
]
_CODE_FILES = ("extract.py", "formula.py", "minhash.py", "index.py")


def _fingerprint(config: Config) -> str:
    """Versão do índice = hash do código que decide o que é guardado + config que muda o conteúdo.

    Qualquer ajuste na detecção (extrator, fórmulas, minhash, esquema) invalida o índice sozinho;
    ninguém precisa lembrar de subir um número.
    """
    h = hashlib.blake2b(digest_size=8)
    for name in _CODE_FILES:
        h.update((Path(__file__).parent / name).read_bytes())
    h.update(repr((config.formula, config.near, config.min_tokens, config.formula_exclude)).encode())
    return h.hexdigest()


def _db_version(db: sqlite3.Connection) -> str | None:
    try:
        row = db.execute("SELECT v FROM meta WHERE k='version'").fetchone()
    except sqlite3.Error:  # sem tabela meta: índice de antes da versão automática
        return None
    return row[0] if row else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha TEXT NOT NULL, stamp TEXT);
CREATE TABLE IF NOT EXISTS functions (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, qualname TEXT NOT NULL,
    lineno INTEGER, end_lineno INTEGER, n_tokens INTEGER,
    h_exact TEXT, h_struct TEXT, sig BLOB, shingles BLOB
);
CREATE TABLE IF NOT EXISTS sdf (h INTEGER PRIMARY KEY, n INTEGER NOT NULL);
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
        version = _fingerprint(self.config)
        if not db_path.exists():
            self._seed_from_worktree(db_path, version)
        self.db = sqlite3.connect(db_path)
        if self.db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] and _db_version(self.db) != version:
            self.db.close()  # índice de outra versão do código/config: recria
            for suffix in ("", "-wal", "-shm"):
                Path(f"{db_path}{suffix}").unlink(missing_ok=True)
            self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('version', ?)", (version,))
        self.db.commit()

    def _seed_from_worktree(self, db_path: Path, version: str) -> None:
        """Worktree nova: copia o índice de outra worktree do mesmo repo (se tiver a mesma config).

        O índice depende só do conteúdo dos arquivos, e o scan compara hash antes de
        reanalisar, então o que for igual à worktree de origem não custa nada.
        """
        try:
            out = subprocess.run(["git", "-C", str(self.root), "worktree", "list", "--porcelain"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return
        others = [Path(line[9:]) / ".cpcheck" / "index.db" for line in out.splitlines() if line.startswith("worktree ")]
        others = [c for c in others if c.exists() and c.parent.parent.resolve() != self.root]
        for src_path in sorted(others, key=lambda c: c.stat().st_mtime, reverse=True):
            try:
                src = sqlite3.connect(src_path)  # backup() lê um retrato consistente, mesmo com scan rodando lá
                try:
                    if _db_version(src) != version:
                        continue
                    dst = sqlite3.connect(db_path)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                    return
                finally:
                    src.close()
            except sqlite3.Error:
                db_path.unlink(missing_ok=True)

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
        if self.config.near:  # desconta os shingles da frequência global
            for (blob,) in self.db.execute("SELECT shingles FROM functions WHERE path=? AND shingles IS NOT NULL", (rel,)):
                self.db.executemany("UPDATE sdf SET n=n-1 WHERE h=?", [(h,) for h in minhash.from_bytes(blob)])
        self.db.execute("DELETE FROM bands WHERE fn_id IN (SELECT id FROM functions WHERE path=?)", (rel,))
        self.db.execute("DELETE FROM formulas WHERE fn_id IN (SELECT id FROM functions WHERE path=?)", (rel,))
        self.db.execute("DELETE FROM functions WHERE path=?", (rel,))
        self.db.execute("DELETE FROM files WHERE path=?", (rel,))

    def _insert(self, rel: str, sha: str, stamp: str, funcs: list[FunctionInfo]) -> None:
        self.db.execute("INSERT OR REPLACE INTO files(path, sha, stamp) VALUES (?, ?, ?)", (rel, sha, stamp))
        for f in funcs:
            sig = minhash.signature(f.tokens) if self.config.near else None
            ids = minhash.shingle_ids(f.tokens) if sig else None
            cur = self.db.execute(
                "INSERT INTO functions(path, qualname, lineno, end_lineno, n_tokens, h_exact, h_struct, sig, shingles)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rel, f.qualname, f.lineno, f.end_lineno, f.n_tokens, f.h_exact, f.h_struct,
                 minhash.to_bytes(sig) if sig else None, minhash.to_bytes(ids) if ids else None),
            )
            if ids:
                self.db.executemany(
                    "INSERT INTO sdf(h, n) VALUES (?, 1) ON CONFLICT(h) DO UPDATE SET n=n+1", [(h,) for h in ids])
            self.db.executemany(
                "INSERT INTO formulas(canon, fn_id, lineno) VALUES (?, ?, ?)",
                [(c, cur.lastrowid, ln) for c, ln in f.formulas] if self.config.formulas_enabled(rel) else [],
            )
            if sig:
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
            self._insert(rel, sha, stamp, extract_functions(source, rel, self.min_tokens, self.config.formula))
            changed += 1
        removed = [p for p in known if p not in seen]
        for rel in removed:
            self._delete(rel)
        self.db.execute("DELETE FROM sdf WHERE n<=0")
        self.db.commit()
        return changed, len(removed)

    # ---- leitura -----------------------------------------------------------
    def _idf(self, ids: set[int]) -> dict[int, float]:
        """Peso log((N+1)/(df+1)) + 1 (suavizado, nunca zero) de cada shingle; df vem da tabela ``sdf``."""
        total = self.db.execute("SELECT count(*) FROM functions").fetchone()[0] + 1
        df: dict[int, int] = {}
        pool = list(ids)
        for i in range(0, len(pool), 500):
            chunk = pool[i : i + 500]
            df.update(self.db.execute(f"SELECT h, n FROM sdf WHERE h IN ({','.join('?' * len(chunk))})", chunk))
        return {h: math.log(total / (df.get(h, 0) + 1)) + 1 for h in ids}

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
                # função aninhada x pai (render_tab._ytd x render_tab) não é duplicata
                nested = path == f.path and (qual.startswith(f.qualname + ".") or f.qualname.startswith(qual + "."))
                if key != own and not nested and key not in found:
                    found[key] = Match(kind, score, path, qual, line)

        add("exact", 1.0, self.db.execute("SELECT path, qualname, lineno FROM functions WHERE h_exact=?", (f.h_exact,)))
        add("struct", 1.0, self.db.execute("SELECT path, qualname, lineno FROM functions WHERE h_struct=?", (f.h_struct,)))
        for canon, _ in (f.formulas if self.config.formula and self.config.formulas_enabled(f.path) else []):
            rows = self.db.execute(
                "SELECT fn.path, fn.qualname, fo.lineno, fn.id FROM formulas fo JOIN functions fn ON fn.id=fo.fn_id"
                " WHERE fo.canon=?", (canon,)).fetchall()
            # fórmula em funções demais é idioma do projeto (ex.: 100 * a / b), não cópia
            if len({r[3] for r in rows}) <= self.config.formula_max_funcs:
                add("formula", 1.0, [r[:3] for r in rows])

        if self.config.near and f.n_tokens >= self.min_tokens and len(found) < limit:
            sig = minhash.signature(f.tokens)
            band_list = minhash.bands(sig)
            marks = ",".join("?" * len(band_list))
            rows = self.db.execute(
                f"SELECT DISTINCT fn.path, fn.qualname, fn.lineno, fn.shingles FROM bands b JOIN functions fn ON fn.id=b.fn_id"
                f" WHERE b.band IN ({marks})",
                band_list,
            ).fetchall()
            # O LSH só acha candidatos. A nota final pondera cada shingle pela raridade no repo
            # (idf), então boilerplate compartilhado por muitas funções (CRUD, try/commit/return)
            # pesa pouco e o conteúdo raro em comum pesa muito.
            mine = set(minhash.shingle_ids(f.tokens))
            cands = [(p, q, ln, set(minhash.from_bytes(b))) for p, q, ln, b in rows if b]
            weight = self._idf(mine.union(*(c[3] for c in cands)))
            near = []
            for path, qual, line, theirs in cands:
                union = sum(weight[h] for h in mine | theirs)
                score = sum(weight[h] for h in mine & theirs) / union if union else 0.0
                if score >= near_threshold:
                    near.append((score, path, qual, line))
            for score, path, qual, line in sorted(near, reverse=True):
                add("near", score, [(path, qual, line)])

        order = {"exact": 0, "struct": 1, "formula": 2, "near": 3}
        return sorted(found.values(), key=lambda m: (order[m.kind], -m.score))[:limit]
