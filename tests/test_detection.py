import io
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from copy_paste_check import hook
from copy_paste_check.cli import main
from copy_paste_check.config import Config
from copy_paste_check.extract import extract_functions
from copy_paste_check.index import Index

ORIGINAL = textwrap.dedent('''
    def media_ponderada(valores, pesos):
        """Média ponderada simples."""
        total = 0
        soma_pesos = 0
        for valor, peso in zip(valores, pesos):
            total += valor * peso
            soma_pesos += peso
        if soma_pesos == 0:
            return None
        return total / soma_pesos


    def retorno_simples(preco_final, preco_inicial):
        return (preco_final - preco_inicial) / preco_inicial
''')


def fn(src: str, name: str):
    return next(f for f in extract_functions(textwrap.dedent(src), "x.py", min_tokens=1) if f.qualname == name)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "calc.py").write_text(ORIGINAL)
    return tmp_path


# ---- impressões digitais -----------------------------------------------------

def test_exact_ignores_name_and_docstring():
    a = fn("def a(x, y):\n    'doc'\n    z = x + y\n    return z * 2\n", "a")
    b = fn("def b(x, y):\n    z = x + y  # comentário\n    return z * 2\n", "b")
    assert a.h_exact == b.h_exact


def test_struct_ignores_variable_names_and_literals():
    a = fn("def a(x, y):\n    z = x + y\n    return z * 2\n", "a")
    b = fn("def b(p, q):\n    s = p + q\n    return s * 3\n", "b")
    assert a.h_exact != b.h_exact
    assert a.h_struct == b.h_struct


def test_struct_keeps_called_functions():
    a = fn("def a(x):\n    return len(x) + 1\n", "a")
    b = fn("def b(x):\n    return sum(x) + 1\n", "b")
    assert a.h_struct != b.h_struct


def canon(src: str, name: str = "r") -> set[str]:
    return {c for c, _ in fn(src, name).formulas}


@pytest.mark.parametrize("other", [
    "def r(a, b):\n    return a / b - 1\n",
    "def r(b, a):\n    return (a - b) / b\n".replace("(a - b) / b", "(b - a) / a"),
    "def r(final, inicial):\n    diff = final - inicial\n    return diff / inicial\n",
])
def test_formula_equivalent_forms(other):
    base = canon("def r(a, b):\n    return (a - b) / b\n")
    assert base
    assert canon(other) == base


def test_formula_log_identity():
    a = canon("import math\ndef r(a, b):\n    return math.log(a) - math.log(b)\n")
    assert a and a == canon("import numpy as np\ndef r(a, b):\n    return np.log(a / b)\n")


def test_formula_different_math_differs():
    assert canon("def r(a, b):\n    return (a - b) / b\n") != canon("def r(a, b):\n    return (a - b) / a + 1\n")


def test_not_a_formula():
    assert canon("def r(a):\n    return a\n") == set()
    assert canon("def r(xs):\n    for i in range(len(xs)):\n        print(xs[i + 1])\n    return 0\n") == set()


def test_formula_pandas_equivalences():
    a = canon("def r(df):\n    df['ret'] = df['close'] / df['close'].shift(1) - 1\n    return df\n")
    b = canon("def r(precos):\n    out = precos.close.pct_change()\n    return out.mean() * 252\n")
    c = canon("def r(p):\n    x = p.close\n    return (x - x.shift()) / x.shift()\n")
    assert a and a <= b | c and a == c


def test_formula_inline_inside_loop():
    src = (
        "def r(rows):\n"
        "    out = []\n"
        "    for row in rows:\n"
        "        out.append((row.final - row.inicial) / row.inicial * 100)\n"
        "    return out\n"
    )
    other = "def s(a, b):\n    return (a / b - 1) * 100\n"
    assert canon(src) & canon(other, "s")


def test_formula_constants_matter():
    anual_252 = canon("def r(d):\n    return (1 + d) ** 252 - 1\n")
    anual_365 = canon("def r(d):\n    return (1 + d) ** 365 - 1\n")
    assert anual_252 and anual_252 != anual_365


def test_syntax_error_returns_empty():
    assert extract_functions("def (:", "x.py") == []


# ---- índice ------------------------------------------------------------------

def test_index_incremental(repo: Path):
    idx = Index(repo)
    assert idx.scan() == (1, 0)
    assert idx.scan() == (0, 0)
    (repo / "shared" / "calc.py").unlink()
    assert idx.scan() == (0, 1)
    idx.close()


def test_query_near_clone(repo: Path):
    (repo / ".cpcheck.toml").write_text("near = true\nnear_threshold = 0.6\n")
    idx = Index(repo)
    idx.scan()
    near = textwrap.dedent('''
        def weighted(vals, ws):
            acc = 0
            wsum = 0
            for v, w in zip(vals, ws):
                acc += v * w
                wsum += w
            if wsum == 0:
                return 0.0
            print("ok")
            return acc / wsum
    ''')
    f = fn(near, "weighted")
    kinds = {m.kind for m in idx.query(f)}
    idx.close()
    assert kinds == {"near"}


# ---- hook --------------------------------------------------------------------

def run_hook(payload: dict) -> dict | None:
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        hook.run(io.StringIO(json.dumps(payload)))
    out = buf.getvalue().strip()
    return json.loads(out) if out else None


def test_hook_blocks_struct_duplicate(repo: Path):
    content = ORIGINAL.replace("media_ponderada", "wavg").replace("valores", "xs").replace("pesos", "ws")
    out = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": str(repo / "novo.py"), "content": content}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shared/calc.py" in reason and "media_ponderada" in reason
    assert len(reason) < 500  # resposta enxuta


def test_hook_silent_when_unique(repo: Path):
    out = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": str(repo / "novo.py"),
                                   "content": "def oi(nome):\n    return f'oi {nome}'\n"}})
    assert out is None


def test_hook_ignores_unchanged_functions_in_edit(repo: Path):
    target = repo / "shared" / "calc.py"
    out = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                    "tool_input": {"file_path": str(target), "old_string": "return None",
                                   "new_string": "return 0.0"}})
    assert out is None


def test_hook_ignore_marker(repo: Path):
    content = "# cpcheck: ignore\n" + ORIGINAL.lstrip().split("\n\n\n")[0].replace("media_ponderada", "wavg")
    out = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": str(repo / "novo.py"), "content": content}})
    assert out is None


def test_hook_warns_formula_post(repo: Path):
    (repo / ".cpcheck.toml").write_text("formula = true\n")
    new = repo / "novo.py"
    new.write_text("def variacao(a, b):\n    return a / b - 1\n")
    out = run_hook({"hook_event_name": "PostToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": str(new), "content": new.read_text()}})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "formula" in ctx and "retorno_simples" in ctx


def test_hook_skips_non_python(repo: Path):
    assert run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                     "tool_input": {"file_path": str(repo / "a.ts"), "content": ORIGINAL}}) is None


# ---- CLI ---------------------------------------------------------------------

def test_cli_diff(repo: Path, capsys):
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / ".git").rmdir()
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (repo / "novo.py").write_text(ORIGINAL.replace("media_ponderada", "wavg"))
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dup")
    code = main(["--root", str(repo), "diff", "--base", "HEAD~1", "--markdown"])
    out = capsys.readouterr().out
    assert code == 1
    assert "wavg" in out and "media_ponderada" in out


def test_cli_diff_sees_uncommitted(repo: Path, capsys):
    """PR em andamento: arquivo não rastreado (sem commit nenhum na branch) também conta."""
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                    check=True, capture_output=True)
    (repo / ".git").rmdir()
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feat")
    (repo / "novo.py").write_text(ORIGINAL.replace("media_ponderada", "wavg"))  # não rastreado
    code = main(["--root", str(repo), "diff", "--base", "main", "--markdown"])
    out = capsys.readouterr().out
    assert code == 1
    assert "wavg" in out and "media_ponderada" in out


def test_scan_skips_nested_checkouts(repo: Path):
    """.worktrees/x (ou submódulo) tem .git próprio: é outro checkout, não código deste."""
    wt = repo / ".worktrees" / "outra"
    (wt / "shared").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../../.git/worktrees/outra\n")
    (wt / "shared" / "calc.py").write_text(ORIGINAL)
    idx = Index(repo)
    idx.scan()
    paths = {p for (p,) in idx.db.execute("SELECT DISTINCT path FROM functions")}
    idx.close()
    assert paths == {"shared/calc.py"}


def test_config_allow_intentional_copies(repo: Path):
    (repo / ".cpcheck.toml").write_text('allow = ["templates/*.py::_parse"]\n')
    tpl = repo / "templates"
    tpl.mkdir()
    body = ORIGINAL.lstrip().split("\n\n\n")[0]
    (tpl / "a.py").write_text(body.replace("media_ponderada", "_parse"))
    idx = Index(repo)
    assert idx.config.allowed("templates/b.py", "_parse")
    idx.scan()
    in_template = fn(body.replace("media_ponderada", "_parse"), "_parse")
    in_template.path = "templates/b.py"
    assert idx.query(in_template) == []  # cópia intencional: não cobra
    outside = fn(body.replace("media_ponderada", "outra"), "outra")
    outside.path = "shared/novo.py"
    assert {m.path for m in idx.query(outside)} >= {"templates/a.py"}  # mas segue como origem a reutilizar
    idx.close()


def test_config_from_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.cpcheck]\nmin_tokens = 10\nexclude = ['pocs/*']\n")
    cfg = Config.load(tmp_path)
    assert cfg.min_tokens == 10 and cfg.exclude == ["pocs/*"]


def test_formula_and_near_off_by_default(repo: Path):
    """Padrão leve: só exact/struct; sem sympy e sem MinHash no índice."""
    idx = Index(repo)
    idx.scan()
    assert not idx.config.formula and not idx.config.near
    assert idx.db.execute("SELECT count(*) FROM formulas").fetchone()[0] == 0
    assert idx.db.execute("SELECT count(*) FROM bands").fetchone()[0] == 0
    idx.close()


def test_formula_idiom_in_many_functions_is_ignored(repo: Path):
    (repo / ".cpcheck.toml").write_text("formula = true\nformula_max_funcs = 2\n")
    for i in range(3):
        (repo / "shared" / f"pct{i}.py").write_text(f"def pct{i}(a, b):\n    return 100 * a / b\n")
    idx = Index(repo)
    idx.scan()
    f = fn("def novo(x, y):\n    return 100 * x / y\n", "novo")
    kinds = {m.kind for m in idx.query(f)}
    idx.close()
    assert "formula" not in kinds


def test_nested_function_is_not_duplicate_of_parent(repo: Path):
    (repo / ".cpcheck.toml").write_text("formula = true\n")
    (repo / "shared" / "n.py").write_text(
        "def outer(r):\n    def inner(r):\n        return (1 + r) ** 3 - 1\n    return (1 + r) ** 3 - 1\n")
    idx = Index(repo)
    idx.scan()
    f = next(f for f in extract_functions((repo / "shared" / "n.py").read_text(), "shared/n.py", 1, True)
             if f.qualname == "outer")
    matches = idx.query(f)
    idx.close()
    assert not any(m.qualname == "outer.inner" for m in matches)


def _crud(name: str, extra: str) -> str:
    return textwrap.dedent(f'''
        def {name}(session, key, payload):
            obj = session.get(Model, key)
            if obj is None:
                raise LookupError(key)
            for field, value in payload.items():
                setattr(obj, field, value)
            {extra}
            session.add(obj)
            session.commit()
            session.refresh(obj)
            return obj
    ''')


def test_near_ignores_boilerplate_shared_by_many_functions(repo: Path):
    (repo / ".cpcheck.toml").write_text("near = true\nnear_threshold = 0.75\n")
    extras = ["log.info(obj)", "audit(obj, user)", "notify(obj.owner)", "cache.drop(key)", "metrics.hit(obj)", "obj.touch()"]
    for i, extra in enumerate(extras):
        (repo / "shared" / f"crud{i}.py").write_text(_crud(f"update_{i}", extra))
    idx = Index(repo)
    idx.scan()
    boilerplate = fn(_crud("update_new", "queue.push(obj)"), "update_new")
    clone_of_rare = fn(_crud("update_copy", extras[0]).replace("session.refresh(obj)", "session.expire(obj)"), "update_copy")
    kinds = {m.kind for m in idx.query(boilerplate)}
    idx.close()
    assert "near" not in kinds
    assert clone_of_rare  # só garante que o helper gera código válido


def test_new_worktree_is_seeded_from_another_one(tmp_path: Path):
    def git(*a, cwd):
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    (main / "calc.py").write_text(ORIGINAL)
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=main)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", cwd=main)
    a = Index(main)
    a.scan()
    n = a.db.execute("SELECT count(*) FROM functions").fetchone()[0]
    a.close()
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), cwd=main)
    b = Index(wt)
    seeded = b.db.execute("SELECT count(*) FROM functions").fetchone()[0]
    changed = b.scan()
    b.close()
    assert n > 0 and seeded == n  # veio pronto, antes de qualquer scan
    assert changed == (0, 0)  # arquivos iguais só atualizam o carimbo


def test_index_recreated_when_detection_code_changes(repo, monkeypatch):
    from copy_paste_check import index as index_mod

    idx = Index(repo)
    idx.scan()
    idx.close()
    idx = Index(repo)  # mesma versão: índice mantido
    assert idx.db.execute("SELECT count(*) FROM functions").fetchone()[0] > 0
    idx.close()
    monkeypatch.setattr(index_mod, "_fingerprint", lambda config: "outra-versao")
    idx = Index(repo)  # código mudou: recria sozinho, vazio
    assert idx.db.execute("SELECT count(*) FROM functions").fetchone()[0] == 0
    idx.close()


def test_changing_min_tokens_recreates_index(repo):
    first = Index(repo)
    first.scan()
    first.close()
    cfg = Config(min_tokens=5)
    idx = Index(repo, cfg)
    assert idx.db.execute("SELECT count(*) FROM functions").fetchone()[0] == 0
    idx.close()


def test_parallel_scan_matches_serial(repo, tmp_path_factory, monkeypatch):
    from copy_paste_check import index as index_mod

    def dump(root: Path):
        idx = Index(root, Config(near=True, min_tokens=1))
        idx.scan()
        rows = idx.db.execute("SELECT path, qualname, h_exact, h_struct, sig FROM functions ORDER BY path, qualname").fetchall()
        idx.close()
        return rows

    serial = dump(repo)
    other = tmp_path_factory.mktemp("par")
    (other / ".git").mkdir()
    (other / "shared").mkdir()
    (other / "shared" / "calc.py").write_text(ORIGINAL)
    monkeypatch.setattr(index_mod, "PARALLEL_MIN_FILES", 1)
    assert dump(other) == serial and serial
