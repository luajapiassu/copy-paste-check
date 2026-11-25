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


def test_formula_iloc_e_cast():
    """``float(s.iloc[-1] / s.iloc[0] - 1)`` é a mesma conta de ``a / b - 1``."""
    base = canon("def r(a, b):\n    return a / b - 1\n")
    assert base
    assert canon("def r(s):\n    return float(s.iloc[-1] / s.iloc[0] - 1)\n") == base
    assert canon("def r(s):\n    return (s.iloc[-1] - s.iloc[0]) / s.iloc[0]\n") == base


def test_formula_cast_nao_esconde_outra_conta():
    a = canon("def r(s):\n    return float(s.iloc[-1] / s.iloc[0] - 1)\n")
    assert a != canon("def r(s):\n    return float(s.iloc[-1] / s.iloc[0] + 1)\n")


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


def _log(repo: Path) -> list[dict]:
    f = repo / ".cpcheck" / "hook.log"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def test_hook_log_registra_execucao_com_achado(repo: Path):
    content = ORIGINAL.replace("media_ponderada", "wavg")
    run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
              "tool_input": {"file_path": str(repo / "novo.py"), "content": content}})
    (rec,) = _log(repo)
    assert rec["evento"] == "PreToolUse" and rec["arquivo"] == "novo.py"
    assert rec["checadas"] >= 1 and rec["achados"] == ["exact"] and rec["nivel"] == "block"


def test_hook_log_registra_silencio_e_funcao_pequena(repo: Path):
    run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
              "tool_input": {"file_path": str(repo / "novo.py"), "content": "def oi(n):\n    return n\n"}})
    (rec,) = _log(repo)
    assert rec["checadas"] == 0 and rec["achados"] == [] and rec["nivel"] == ""


def test_hook_log_ignora_nao_python(repo: Path):
    run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
              "tool_input": {"file_path": str(repo / "a.ts"), "content": "x"}})
    assert _log(repo) == []


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


def test_padrao_near_ligado_e_formula_desligada(repo: Path):
    """Padrão: exact/struct + near (cópia com linhas alteradas); formula (sympy) é opcional."""
    idx = Index(repo)
    idx.scan()
    assert not idx.config.formula and idx.config.near
    assert idx.db.execute("SELECT count(*) FROM formulas").fetchone()[0] == 0
    assert idx.db.execute("SELECT count(*) FROM bands").fetchone()[0] > 0
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


# ---- gaps achados na avaliação de 2026-11-14 ----------------------------------

FORMA = '''
def {n}(df, col):
    out = df[df[col] > {a}].copy()
    out["{c1}"] = out[col].rolling({w}).mean() * {k}
    out["{c2}"] = out["{c1}"].fillna({a})
    out = out.sort_values("{c1}").reset_index(drop=True)
    return out[["{c1}", "{c2}"]]
'''


def _pre(tool: str, **inp) -> dict | None:
    return run_hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp})


def test_hook_nao_bloqueia_renomear_funcao(repo: Path):
    """A versão antiga ainda está no índice na hora do PreToolUse: não é duplicata, é a própria função."""
    target = repo / "shared" / "calc.py"
    assert _pre("Edit", file_path=str(target), old_string="def media_ponderada(", new_string="def wavg(") is None


def test_hook_mover_funcao_bloqueia_mas_diz_como(repo: Path):
    out = _pre("Write", file_path=str(repo / "novo.py"), content=ORIGINAL)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "movendo" in reason and len(reason) < 500


def test_struct_com_literais_diferentes_avisa_em_vez_de_bloquear(repo: Path):
    (repo / "a.py").write_text(FORMA.format(n="vol", a=0, c1="vol", c2="vol_f", w=21, k=252))
    outra = FORMA.format(n="liquidez", a=100, c1="liq", c2="liq_f", w=63, k=365)
    assert _pre("Write", file_path=str(repo / "b.py"), content=outra) is None
    (repo / "b.py").write_text(outra)
    out = run_hook({"hook_event_name": "PostToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": str(repo / "b.py"), "content": outra}})
    assert "struct~" in out["hookSpecificOutput"]["additionalContext"]
    # mesmos literais, só nomes trocados: continua bloqueando
    copia = FORMA.format(n="vol2", a=0, c1="vol", c2="vol_f", w=21, k=252).replace("out", "res")
    assert _pre("Write", file_path=str(repo / "c.py"), content=copia)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_vocabulario_separa_camel_case():
    from copy_paste_check.extract import _words
    assert _words("annualizedVol AnbimaCalendar") == ["annualized", "vol", "anbima", "calendar"]


def test_arquivo_de_teste_e_pelo_padrao_nao_por_conter_test():
    from copy_paste_check.config import is_test_path
    assert not is_test_path("shared/quant_tools/backtest.py") and not is_test_path("apps/latest.py")
    assert all(is_test_path(p) for p in ("tests/unit/helpers.py", "apps/api/tests/test_x.py", "test_a.py",
                                         "a/b_test.py", "conftest.py"))


def test_vocab_acha_canonica_em_backtest_py(repo: Path):
    (repo / "backtest.py").write_text('def vol_anual(retornos):\n    """volatilidade anualizada"""\n'
                                      '    return retornos.std() * 252 ** 0.5\n')
    idx = Index(repo)
    idx.scan()
    nova = fn('def volatilidade(retornos):\n    """volatilidade anualizada"""\n    return retornos.std(ddof=0) * 252 ** 0.5\n',
              "volatilidade")
    assert "vol_anual" in [m.qualname for m in idx.by_vocab(nova)]
    idx.close()


def test_scan_interrompido_guarda_o_que_ja_fez(repo: Path, monkeypatch):
    """Hook com timeout mata o primeiro scan: o próximo continua de onde parou, não do zero."""
    from copy_paste_check import index as index_mod
    for i in range(5):
        (repo / f"m{i}.py").write_text(ORIGINAL.replace("media_ponderada", f"f{i}"))
    monkeypatch.setattr(index_mod, "COMMIT_EVERY", 2)
    real, seen = index_mod._analyze, []

    def morre_no_quinto(job):
        seen.append(job[0])
        if len(seen) == 5:
            raise KeyboardInterrupt
        return real(job)

    monkeypatch.setattr(index_mod, "_analyze", morre_no_quinto)
    idx = Index(repo)
    with pytest.raises(KeyboardInterrupt):
        idx.scan()
    idx.close()
    monkeypatch.setattr(index_mod, "_analyze", real)
    idx = Index(repo)
    assert idx.db.execute("SELECT count(*) FROM files").fetchone()[0] == 4
    assert idx.scan()[0] == 2  # só os que faltavam
    idx.close()


def test_config_avisa_chave_desconhecida(repo: Path, capsys):
    (repo / ".cpcheck.toml").write_text("near_treshold = 0.9\n")
    Config.load(repo)
    assert "near_treshold" in capsys.readouterr().err


def test_pasta_do_indice_se_ignora_sozinha(repo: Path):
    Index(repo).close()
    assert (repo / ".cpcheck" / ".gitignore").read_text().strip() == "*"


def test_formula_kwargs_mudam_a_conta():
    """std(ddof=0) e std() dão resultados diferentes: não podem ter a mesma forma canônica."""
    a = canon("def r(x):\n    return x.std(ddof=0) * 252 ** 0.5\n")
    b = canon("def r(x):\n    return x.std() * 252 ** 0.5\n")
    c = canon("def r(y):\n    return y.std(ddof=0) * 252 ** 0.5\n")
    assert a and a != b and a == c


def test_diff_nao_calcula_formula_com_formula_desligada(repo: Path, monkeypatch, capsys):
    from copy_paste_check import extract
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                    check=True, capture_output=True)
    (repo / ".git").rmdir()
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "base")
    (repo / "novo.py").write_text(ORIGINAL.replace("media_ponderada", "wavg"))
    monkeypatch.setattr(extract, "function_formulas", lambda fn: 1 / 0)
    assert main(["--root", str(repo), "diff", "--base", "main"]) == 1
    capsys.readouterr()


def test_arquivo_renomeado_nao_vira_funcao_nova(repo: Path):
    from copy_paste_check.cli import review_input
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                    check=True, capture_output=True)
    (repo / ".git").rmdir()
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("mv", "shared/calc.py", "shared/contas.py")
    (repo / "shared" / "contas.py").write_text(ORIGINAL.replace("return None", "return None if pesos else 0"))
    idx = Index(repo)
    idx.scan()
    changed, novas = review_input(idx, "main")
    idx.close()
    assert novas == set()
    assert [f.qualname for _, funcs in changed for f in funcs] == ["media_ponderada"]


def test_post_write_usa_o_antes_que_o_claude_code_manda(repo: Path):
    """Reescrever arquivo existente sem mexer na função não repete o aviso (tool_response.originalFile é o antes)."""
    (repo / ".cpcheck.toml").write_text("formula = true\n")
    new = repo / "novo.py"
    velho = "def variacao(a, b):\n    return a / b - 1\n"
    new.write_text(velho + "\n\nX = 1\n")
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Write",
               "tool_input": {"file_path": str(new), "content": new.read_text()}}
    assert run_hook({**payload, "tool_response": {"originalFile": velho}}) is None
    assert run_hook({**payload, "tool_response": {"originalFile": None}}) is not None  # arquivo novo: avisa


def test_hook_checa_mesmo_com_indice_travado_por_outro_hook(repo: Path, monkeypatch):
    """Dois hooks no mesmo checkout: o segundo não consegue gravar, mas consulta o índice que já existe."""
    import sqlite3
    from copy_paste_check import index as index_mod
    idx = Index(repo)
    idx.scan()
    idx.close()
    monkeypatch.setattr(index_mod, "LOCK_WAIT", 0.2)
    outro = sqlite3.connect(repo / ".cpcheck" / "index.db")
    outro.execute("BEGIN IMMEDIATE")
    try:
        out = _pre("Write", file_path=str(repo / "novo.py"), content=ORIGINAL.replace("media_ponderada", "wavg"))
    finally:
        outro.rollback()
        outro.close()
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
