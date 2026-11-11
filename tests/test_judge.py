import json
import textwrap
from pathlib import Path

from copy_paste_check import judge
from copy_paste_check.config import Config
from copy_paste_check.extract import extract_functions
from copy_paste_check.index import Index, Match

A = textwrap.dedent('''
    def vol(rets, n):
        janela = rets[-n:]
        media = sum(janela) / len(janela)
        var = sum((r - media) ** 2 for r in janela) / (len(janela) - 1)
        return var ** 0.5 * 252 ** 0.5
''')
B = A.replace("def vol(rets, n)", "def volatilidade(retornos, k)").replace("(len(janela) - 1)", "len(janela)")


def _setup(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text(A)
    (tmp_path / "b.py").write_text(B)
    idx = Index(tmp_path, Config(min_tokens=1))
    mine = extract_functions(B, "b.py", 1, False)[0]
    match = Match("near", 0.78, "a.py", "vol", 2)
    return idx, [("b.py", "volatilidade", mine.lineno, [match])]


def test_ambiguous_goes_to_model_then_memory(tmp_path):
    idx, results = _setup(tmp_path)
    calls = []

    def runner(prompt, system, tools, cwd):
        calls.append((tools, cwd))
        assert "def vol(" in prompt and "def volatilidade(" in prompt
        return 'claro: {"veredito": "coincidencia", "canonica": "a.py:2", "diferencas": "ddof 1 x 0"}'

    v = judge.judge(idx, results, runner=runner)
    assert [(x.veredito, x.origem) for x in v] == [("coincidencia", "haiku")]
    assert calls == [(None, None)]  # sem ferramentas: julgamento puro, só leitura
    assert json.loads((tmp_path / judge.VERDICTS_FILE).read_text())  # gravou

    v = judge.judge(idx, results, runner=lambda *a: 1 / 0)  # segunda vez: memória, sem chamar
    assert [(x.veredito, x.origem) for x in v] == [("coincidencia", "memoria")]
    idx.close()


def test_memory_key_changes_when_code_changes(tmp_path):
    idx, results = _setup(tmp_path)
    judge.judge(idx, results, runner=lambda *a: '{"veredito": "duplicata"}')
    (tmp_path / "b.py").write_text(B.replace("0.5", "0.25"))
    asked = []
    judge.judge(idx, results, runner=lambda *a: asked.append(1) or '{"veredito": "duplicata"}')
    assert asked  # código mudou, par julgado de novo
    idx.close()


def test_strong_matches_skip_model_and_failures_are_uncertain(tmp_path):
    idx, results = _setup(tmp_path)
    path, qual, line, _ = results[0]
    strong = [(path, qual, line, [Match("struct", 1.0, "a.py", "vol", 2), Match("near", 0.9, "a.py", "vol", 2)])]
    v = judge.judge(idx, strong, runner=lambda *a: 1 / 0)
    assert [x.origem for x in v] == ["regra", "regra"] and {x.veredito for x in v} == {"duplicata"}

    def broken(*a):
        raise RuntimeError("sem rede")

    v = judge.judge(idx, results, runner=broken)
    assert v[0].veredito == "incerto"  # nunca trava nem inventa veredito
    assert not (tmp_path / judge.VERDICTS_FILE).exists()
    idx.close()


def test_ask_is_read_only_tools(tmp_path):
    seen = {}
    judge.ask(tmp_path, ["tirar acento"], runner=lambda p, s, t, c: seen.update(t=t, c=c) or "1 -> NAO EXISTE")
    assert seen["t"] == "Grep,Glob,Read" and seen["c"] == tmp_path
