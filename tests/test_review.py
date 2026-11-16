"""Chamada única (busca local + Haiku sem ferramentas) e impressão digital por execução."""
import os
import sys
import textwrap
from pathlib import Path

import pytest

from copy_paste_check import execfp, judge
from copy_paste_check.config import Config
from copy_paste_check.extract import extract_functions
from copy_paste_check.index import Index

EXISTENTE = textwrap.dedent('''
    def vol_trailing(col, days):
        """Vol anualizada dos retornos numa janela de dias."""
        rets = col.pct_change().dropna()
        return float(rets.std(ddof=1) * (252 ** 0.5))


    def fold(texto):
        sem_acento = unicodedata.normalize("NFKD", texto or "")
        return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold().strip()


    def salvar_cliente(session, cliente):
        session.add(cliente)
        session.commit()
        return cliente
''')

NOVA = textwrap.dedent('''
    def _vol_anual(precos):
        r = precos.pct_change().dropna()
        return float(r.std() * 252 ** 0.5)


    def test_vol_anual():
        assert _vol_anual is not None
''')


@pytest.fixture
def idx(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "calc.py").write_text(EXISTENTE, encoding="utf-8")
    i = Index(tmp_path)
    i.scan()
    yield i
    i.close()


def _changed():
    return [("novo.py", extract_functions(NOVA, "novo.py", 1, False))]


def test_vocab_acha_funcao_com_outro_nome(idx):
    nova = _changed()[0][1][0]
    nomes = [m.qualname for m in idx.by_vocab(nova)]
    assert nomes[0] == "vol_trailing"
    assert "fold" in nomes or len(nomes) < 3  # função curta também está no vocabulário


def test_review_uma_chamada_sem_ferramentas_e_sem_testes(idx):
    calls = []

    def runner(prompt, system, tools, cwd):
        calls.append(tools)
        assert "_vol_anual" in prompt and "def test_vol_anual" not in prompt
        assert "vol_trailing" in prompt
        return "1 -> [USAR shared/calc.py:2 vol_trailing] - mesma conta"

    out = judge.review(idx, _changed(), runner=runner, execute=False)
    assert calls == [None]
    assert [(f.qualname, f.canonica.qualname, f.origem) for f in out] == [("_vol_anual", "vol_trailing", "haiku")]


def test_review_descarta_resposta_fora_das_candidatas(idx):
    def runner(prompt, system, tools, cwd):  # aponta a própria função nova
        return "Hmm.\n1 -> [USAR novo.py:2 _vol_anual] - idêntica"

    out = judge.review(idx, _changed(), runner=runner, execute=False)
    assert [(f.canonica, f.origem, f.ressalva) for f in out] == [(None, "haiku", "apontou a própria função")]
    inventada = judge.review(idx, _changed(), runner=lambda *a: "1 -> [USAR x.py:9 nao_listada] - y", execute=False)
    assert [(f.canonica, f.origem) for f in inventada] == [(None, "descartada")]


def test_review_nao_existe(idx):
    out = judge.review(idx, _changed(), runner=lambda *a: "1 -> [NAO EXISTE] - nada igual", execute=False)
    assert [(f.canonica, f.origem) for f in out] == [(None, "haiku")]


def test_review_funcao_alterada_so_vai_com_par_do_indice(idx):
    """Duplicata de função que já existia não foi criada pelo PR: sem par no índice, não gasta chamada."""
    def runner(*a):
        raise AssertionError("não devia chamar")

    assert judge.review(idx, _changed(), runner=runner, execute=False, novas=set()) == []


def test_review_ressalva_que_se_corrige_vira_nao_existe(idx):
    def runner(*a):
        return "1 → [USAR shared/calc.py:2 vol_trailing] - Não: outra coisa. Resposta correta: NAO EXISTE."

    out = judge.review(idx, _changed(), runner=runner, execute=False)
    assert [(f.canonica, f.origem) for f in out] == [(None, "haiku")]


def test_review_repete_so_os_itens_sem_resposta(idx):
    dois = NOVA + textwrap.dedent('''

        def _tira_acento(s):
            n = unicodedata.normalize("NFKD", s or "")
            return "".join(c for c in n if not unicodedata.combining(c)).lower().strip()
    ''')
    changed = [("novo.py", extract_functions(dois, "novo.py", 1, False))]
    prompts = []

    def runner(prompt, *a):
        prompts.append(prompt)
        if len(prompts) == 1:
            assert "2 itens" in prompt
            return "1 -> [USAR shared/calc.py:2 vol_trailing] - mesma conta"  # parou no 1
        assert "_tira_acento" in prompt and "_vol_anual" not in prompt
        return "1 -> [USAR shared/calc.py:8 fold] - lower x casefold"

    out = judge.review(idx, changed, runner=runner, execute=False)
    assert len(prompts) == 2
    assert {(f.qualname, f.canonica.qualname) for f in out} == {("_vol_anual", "vol_trailing"), ("_tira_acento", "fold")}


def test_vocab_nao_oferece_outra_funcao_do_proprio_pr(idx):
    """O índice já tem a working tree: função nova do PR não é 'candidata existente' de outra nova."""
    irmas = NOVA + textwrap.dedent('''

        def _vol_anual_janela(precos, dias):
            r = precos.pct_change().dropna().tail(dias)
            return float(r.std() * 252 ** 0.5)
    ''')
    (idx.root / "novo.py").write_text(irmas, encoding="utf-8")
    idx.scan()
    changed = [("novo.py", extract_functions(irmas, "novo.py", 1, False))]
    prompts = []
    judge.review(idx, changed, runner=lambda p, *a: prompts.append(p) or "", execute=False)
    item1 = prompts[0].split("## 2.")[0]
    assert "- novo.py" not in item1 and "vol_trailing" in item1


def test_parse_aceita_os_formatos_que_o_haiku_devolveu():
    """Respostas reais (2026-11-12): o formato varia entre chamadas."""
    real = textwrap.dedent('''
        1. _salvar_chave -> [USAR api/routers/arquivos.py:365 _salvar_chave] - Mesma gravação; duplicada.
        6. _pdf_aberto -> [USAR tests/unit/test_relatorios.py:201 _pdf_valido] - Mesmo PDF.
        2 -> NAO EXISTE - chaves_do_cliente devolve só os valores.
        3 → [NÃO EXISTE] – delete_report tem o mesmo esqueleto.
        **4** -> [USAR shared/x.py:1 f] - sem acento
        Espera, reviso a linha 1 abaixo.
        1 -> [NAO EXISTE] - corrigido
    ''')
    got = judge._parse_review(real)
    assert got[1][0].startswith("NAO EXISTE") and got[2][0] == "NAO EXISTE" and "EXISTE" in got[3][0]
    assert "_pdf_valido" in got[6][0] and "x.py:1 f" in got[4][0]
    assert got[2][1] == "chaves_do_cliente devolve só os valores."


def test_review_sem_funcao_nova_nao_chama(idx):
    def runner(*a):
        raise AssertionError("não devia chamar")

    so_teste = [("t.py", extract_functions("def test_x():\n    assert 1 + 1 == 2\n", "t.py", 1, False))]
    assert judge.review(idx, so_teste, runner=runner, execute=False) == []


# ---- execução -----------------------------------------------------------------

def test_pura_recusa_io_import_e_metodo():
    import ast
    ok = ast.parse("def f(x):\n    return math.sqrt(x) * 2\n").body[0]
    io = ast.parse("def f(x):\n    return open(x).read() + 1\n").body[0]
    imp = ast.parse("def f(x):\n    import os\n    return x + 1\n").body[0]
    met = ast.parse("def f(self, x):\n    return x + 1\n").body[0]
    sess = ast.parse("def f(x):\n    return session.query(x) + 1\n").body[0]
    assert execfp.pure(ok) and not any(execfp.pure(n) for n in (io, imp, met, sess))


VOLS = textwrap.dedent('''
    def vol_trailing(col, days):
        series = col
        if days is not None:
            series = col.loc[col.index >= col.index[-1] - pd.Timedelta(days=days)]
        rets = series.pct_change().dropna()
        return None if len(rets) < 5 else float(rets.std(ddof=1) * (252 ** 0.5))
''')


def test_execucao_acha_mesma_conta_e_quase(tmp_path: Path):
    pytest.importorskip("pandas")
    (tmp_path / "w.py").write_text(VOLS, encoding="utf-8")
    nova = textwrap.dedent('''
        def _vol_anual(precos, dias=None):
            if dias:
                precos = precos[precos.index >= precos.index[-1] - pd.Timedelta(days=dias)]
            r = precos.pct_change().dropna()
            return float(r.std() * 252 ** 0.5) if len(r) >= 5 else None


        def _vol_pop(precos, dias=None):
            if dias:
                precos = precos[precos.index >= precos.index[-1] - pd.Timedelta(days=dias)]
            r = precos.pct_change().dropna()
            return float(r.std(ddof=0) * 252 ** 0.5) if len(r) >= 5 else None
    ''')
    found = execfp.matches(tmp_path, ["w.py"], {"novo.py": nova}, python=sys.executable)
    got = {(k[1], m.qualname, m.kind) for k, ms in found.items() for m in ms}
    assert ("_vol_anual", "vol_trailing", "execucao") in got
    assert ("_vol_pop", "vol_trailing", "execucao~") in got


def test_ask_is_read_only_tools(tmp_path):
    seen = {}
    judge.ask(tmp_path, ["tirar acento"], runner=lambda p, s, t, c: seen.update(t=t, c=c) or "1 -> NAO EXISTE")
    assert seen["t"] == "Grep,Glob,Read" and seen["c"] == tmp_path


# ---- gaps achados na avaliação de 2026-11-14 ----------------------------------

def test_execucao_nao_confunde_contas_de_mesma_soma(tmp_path: Path):
    """zscore e demean somam 0; ordenar não muda a soma. A impressão não pode ser só a soma."""
    pytest.importorskip("pandas")
    (tmp_path / "a.py").write_text("def demean(x):\n    return x - x.mean()\n\n\ndef vezes_um(x):\n    return x * 1.0\n")
    nova = ("def zscore(serie):\n    return (serie - serie.mean()) / serie.std()\n\n\n"
            "def ordena(x):\n    return np.sort(x) * 1.0\n\n\n"
            "def tira_media(s):\n    m = s.mean()\n    return s - m\n")
    found = execfp.matches(tmp_path, ["a.py"], {"novo.py": nova}, python=sys.executable)
    assert {(k[1], m.qualname, m.kind) for k, ms in found.items() for m in ms} == {("tira_media", "demean", "execucao")}


def test_pura_recusa_laco_sem_fim_e_io_do_pandas():
    import ast
    laco = ast.parse("def f(x):\n    n = 0\n    while True:\n        n = n + 1\n    return n\n").body[0]
    html = ast.parse("def f(x):\n    return pd.read_html(x)[0] * 2\n").body[0]
    feather = ast.parse("def f(x):\n    x.to_feather('a')\n    return x * 2\n").body[0]
    savez = ast.parse("def f(x):\n    np.savez('a', x)\n    return x * 2\n").body[0]
    ok = ast.parse("def f(x):\n    return x.to_numpy() * 2\n").body[0]
    assert execfp.pure(ok) and not any(execfp.pure(n) for n in (laco, html, feather, savez))


def test_python_do_projeto_vem_do_checkout_principal_na_worktree(tmp_path: Path):
    """Worktree não tem .venv própria: usa a do checkout principal em vez do Python do cpcheck."""
    import subprocess
    main = tmp_path / "main"
    main.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                    check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (main / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat")
    exe = main / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert Path(execfp.find_python(tmp_path / "wt")) == exe


def test_execucao_avisa_quando_nao_roda(tmp_path: Path, capsys):
    (tmp_path / "a.py").write_text("def f(x):\n    return x * 2\n")
    assert execfp.matches(tmp_path, ["a.py"], {"n.py": "def g(x):\n    return x * 2\n"}, python="nao-existe-python") == {}
    assert "execução pulada" in capsys.readouterr().err


def test_quase_igual_na_execucao_vai_ao_modelo_como_candidata(idx, monkeypatch):
    """execucao~ (até 5%) é pista, não veredito: quem decide é o Haiku."""
    from copy_paste_check.index import Match
    monkeypatch.setattr(execfp, "matches", lambda *a, **k: {
        ("novo.py", "_vol_anual"): [Match("execucao~", 6, "shared/calc.py", "vol_trailing", 2)]})
    (idx.root / "novo.py").write_text(NOVA, encoding="utf-8")
    prompts = []

    def runner(prompt, *a):
        prompts.append(prompt)
        return "1 -> NAO EXISTE - ddof diferente"

    out = judge.review(idx, _changed(), runner=runner, novas=set())
    assert "quase igual" in prompts[0] and [(f.canonica, f.origem) for f in out] == [(None, "haiku")]
    monkeypatch.setattr(execfp, "matches", lambda *a, **k: {
        ("novo.py", "_vol_anual"): [Match("execucao", 6, "shared/calc.py", "vol_trailing", 2)]})
    out = judge.review(idx, _changed(), runner=lambda *a: 1 / 0, novas=set())
    assert [(f.canonica.qualname, f.origem) for f in out] == [("vol_trailing", "execucao")]


def test_ressalva_com_nao_existe_nao_derruba_o_usar(idx):
    def runner(*a):
        return "1 -> USAR shared/calc.py:2 vol_trailing - o parâmetro days não existe na nova"

    out = judge.review(idx, _changed(), runner=runner, execute=False)
    assert [f.canonica.qualname for f in out] == ["vol_trailing"]


def test_candidata_vai_sem_docstring_longa(idx):
    doc = "\n".join(f"        linha {i} da explicação" for i in range(20))
    (idx.root / "shared" / "calc.py").write_text(
        f'def vol_trailing(col, days):\n    """Vol anualizada dos retornos.\n\n{doc}\n    """\n'
        "    rets = col.pct_change().dropna()\n    return float(rets.std(ddof=1) * (252 ** 0.5))\n", encoding="utf-8")
    idx.scan()
    prompts = []
    judge.review(idx, _changed(), runner=lambda p, *a: prompts.append(p) or "", execute=False)
    assert "rets.std(ddof=1)" in prompts[0] and "Vol anualizada dos retornos." in prompts[0]
    assert "linha 5 da explicação" not in prompts[0]


def test_cache_da_resposta_nao_chama_duas_vezes(idx):
    calls = []

    def runner(prompt, *a):
        calls.append(1)
        return "1 -> [USAR shared/calc.py:2 vol_trailing] - mesma conta"

    for _ in range(2):
        out = judge.review(idx, _changed(), runner=judge.cached(idx, runner), execute=False)
        assert [f.canonica.qualname for f in out] == ["vol_trailing"]
    assert len(calls) == 1


def test_run_claude_erro_do_cli_nao_vira_resposta(monkeypatch):
    import subprocess
    fake = subprocess.CompletedProcess([], 0, stdout='{"is_error": true, "result": "usage limit reached"}', stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match="usage limit"):
        judge.run_claude("oi", "sys", None, None)


def test_bench_planta_fora_do_repo_e_confere_gabarito(idx, tmp_path):
    """Gabarito é um arquivo do repositório analisado (fica fora deste repo); os casos são plantados em .cpcheck/."""
    from copy_paste_check import bench
    gab = tmp_path / "gabarito.toml"
    gab.write_text(textwrap.dedent("""
        [[caso]]
        espera = ["vol_trailing"]
        codigo = '''
        def _vol_anual(precos):
            r = precos.pct_change().dropna()
            return float(r.std() * 252 ** 0.5)
        '''

        [[caso]]
        espera = []
        codigo = '''
        def _tira_acento(s):
            n = unicodedata.normalize("NFKD", s or "")
            return "".join(c for c in n if not unicodedata.combining(c)).lower().strip()
        '''
    """), encoding="utf-8")

    def runner(prompt, *a):  # acerta o 1 e erra o 2 (era para ser NAO EXISTE)
        return "1 -> USAR shared/calc.py:2 vol_trailing - igual\n2 -> USAR shared/calc.py:8 fold - lower x casefold"

    rows = bench.run(idx, bench.load(gab), runner=runner, execute=False)
    assert [(r["nome"], r["ok"], r["achou"]) for r in rows] == [("_vol_anual", True, "vol_trailing"),
                                                              ("_tira_acento", False, "fold")]
    idx.scan()
    assert not [p for (p,) in idx.db.execute("SELECT path FROM files") if "bench" in p]
    assert not list((idx.root / ".cpcheck" / "bench").glob("*.py"))  # limpa o que plantou


def test_candidata_longa_mostra_o_final(idx):
    """Cortar nas primeiras linhas escondia o return, que é onde está a conta."""
    meio = "\n".join(f"    passo_{i} = col * {i}" for i in range(20))
    (idx.root / "shared" / "calc.py").write_text(
        f"def vol_trailing(col, days):\n{meio}\n    rets = col.pct_change().dropna()\n"
        "    return float(rets.std(ddof=1) * (252 ** 0.5))\n", encoding="utf-8")
    idx.scan()
    prompts = []
    judge.review(idx, _changed(), runner=lambda p, *a: prompts.append(p) or "", execute=False)
    assert "rets.std(ddof=1)" in prompts[0] and "passo_15" not in prompts[0]


def test_execucao_acha_conta_que_so_roda_em_retornos(tmp_path: Path):
    """Semidesvio só dá número em série com negativos: precisa de mais de uma entrada desse tipo."""
    pytest.importorskip("pandas")
    (tmp_path / "perf.py").write_text(
        "def semideviation(returns):\n    neg = returns[returns < 0]\n    sq = neg**2\n"
        "    n = np.sum(returns < 0)\n    return (np.sum(sq) / n) ** 0.5\n")
    nova = "def _desvio_negativo(rets):\n    neg = rets[rets < 0]\n    return float(np.sqrt((neg ** 2).sum() / len(neg)))\n"
    found = execfp.matches(tmp_path, ["perf.py"], {"novo.py": nova}, python=sys.executable)
    assert {(k[1], m.qualname, m.kind) for k, ms in found.items() for m in ms} == {
        ("_desvio_negativo", "semideviation", "execucao")}


def test_termos_de_busca_do_modelo_acham_nome_em_outro_idioma(idx):
    """Nova em português, existente é uma sigla em inglês: só o vocabulário não liga uma à outra."""
    (idx.root / "shared" / "perf.py").write_text(
        "def moic(df):\n    return -df.paid_out / df.paid_in\n", encoding="utf-8")
    idx.scan()
    nova = ("def _multiplo_do_capital(fluxos):\n    saidas = fluxos.loc[fluxos['valor'] < 0, 'valor'].sum()\n"
            "    entradas = fluxos.loc[fluxos['valor'] > 0, 'valor'].sum()\n    return -saidas / entradas\n")
    (idx.root / "novo.py").write_text(nova, encoding="utf-8")
    changed = [("novo.py", extract_functions(nova, "novo.py", 1, False))]
    seen = []

    def runner(prompt, system, tools, cwd):
        seen.append(system)
        if system == judge.EXPAND_SYSTEM:
            return "1: moic, multiple, invested, capital"
        assert "def moic" in prompt
        return "1 -> USAR shared/perf.py:1 moic - mesma razão"

    sem = judge.review(idx, changed, runner=lambda p, s, *a: "" if "def moic" not in p else 1 / 0, execute=False)
    assert sem == []  # sem os termos, moic nem vira candidata (nada a perguntar)
    out = judge.review(idx, changed, runner=runner, execute=False, expand=True)
    assert seen == [judge.EXPAND_SYSTEM, judge.REVIEW_SYSTEM] and [f.canonica.qualname for f in out] == ["moic"]


def test_resposta_que_aponta_candidata_de_outro_item_vale(idx):
    """O modelo viu o código dela na mesma chamada: existe e foi mostrada, só estava listada em outro item."""
    duas = NOVA + textwrap.dedent('''

        def _guardar(sessao, obj):
            sessao.add(obj)
            sessao.commit()
            return obj
    ''')
    changed = [("novo.py", extract_functions(duas, "novo.py", 1, False))]
    (idx.root / "novo.py").write_text(duas, encoding="utf-8")

    def runner(prompt, *a):
        um, dois = prompt.split("## 2.")
        assert "salvar_cliente" in dois
        return "1 -> NAO EXISTE - nada\n2 -> USAR shared/calc.py:13 salvar_cliente - igual"

    out = judge.review(idx, changed, runner=runner, execute=False, k=1)
    assert [f.canonica.qualname for f in out if f.canonica] == ["salvar_cliente"]
    trocado = judge.review(idx, changed, runner=lambda *a: "1 -> USAR shared/calc.py:13 salvar_cliente - x\n2 -> NAO EXISTE - y",
                           execute=False, k=1)
    assert [(f.qualname, f.canonica.qualname) for f in trocado if f.canonica] == [("_vol_anual", "salvar_cliente")]


def test_ask_local_sem_modelo(tmp_path, capsys, monkeypatch):
    """`cpcheck ask` responde pelo vocabulário do índice, sem chamar o modelo."""
    from copy_paste_check import cli
    (tmp_path / ".git").mkdir()
    (tmp_path / "calc.py").write_text('def vol_anualizada(retornos):\n    """Volatilidade anualizada."""\n'
                                      "    return retornos.std() * 252 ** 0.5\n", encoding="utf-8")
    monkeypatch.setattr(judge, "run_claude", lambda *a: pytest.fail("não era para chamar o modelo"))
    assert cli.main(["--root", str(tmp_path), "ask", "volatilidade dos retornos", "enviar e-mail"]) == 0
    out = capsys.readouterr().out
    assert "calc.py:1 vol_anualizada" in out and "enviar e-mail: nada" in out


@pytest.mark.skipif(not os.environ.get("CPCHECK_LIVE"), reason="chama o Claude de verdade: CPCHECK_LIVE=1")
def test_chamada_minima_fica_isolada():
    """Se uma versão nova do CLI voltar a carregar MCPs/settings, a chamada mínima passa de ~1 mil para ~100 mil tokens."""
    judge.USAGE.clear()
    judge.run_claude("Responda só: ok", "Responda só: ok", None, None)
    u = judge.USAGE[-1]
    assert u["input"] + u["cache_write"] + u["cache_read"] < 5000, u


def test_parse_review_correcao_no_meio_do_texto():
    """O modelo às vezes pensa alto: vale o último veredito de cada item, mesmo no meio de uma frase."""
    text = ("7 -> NAO EXISTE - pondera por PL\n\nCorreção do item 7: é a mesma informação. Logo: 7 -> USAR a/b.py:86\n\n"
            "8 -> USAR a/b.py:86 make - não; ver abaixo.\n\nCorreção do item 8: divide. Veredito: 8 -> NAO EXISTE - multiplica")
    got = judge._parse_review(text)
    assert got[7][0] == "USAR a/b.py:86" and "EXISTE" in got[8][0]
    from copy_paste_check.index import Match
    cands = [Match("vocab", 1.0, "a/b.py", "make", 86), Match("vocab", 1.0, "a/c.py", "outra", 3)]
    assert judge._pick(got[7][0], cands).qualname == "make"
