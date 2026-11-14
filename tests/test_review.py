"""Chamada única (busca local + Haiku sem ferramentas) e impressão digital por execução."""
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
        1. _salvar_senha -> [USAR apps/api/routers/extratos.py:365 _salvar_senha] - Mesma gravação; duplicada.
        6. _pdf_aberto -> [USAR tests/unit/shared/test_relatorios_clientes.py:201 _pdf_valido] - Mesmo PDF.
        2 -> NAO EXISTE - senhas_do_cliente devolve só os valores.
        3 → [NÃO EXISTE] – delete_report tem o mesmo esqueleto.
        **4** -> [USAR shared/x.py:1 f] - sem acento
        Espera, reviso a linha 1 abaixo.
        1 -> [NAO EXISTE] - corrigido
    ''')
    got = judge._parse_review(real)
    assert got[1][0].startswith("NAO EXISTE") and got[2][0] == "NAO EXISTE" and "EXISTE" in got[3][0]
    assert "_pdf_valido" in got[6][0] and "x.py:1 f" in got[4][0]
    assert got[2][1] == "senhas_do_cliente devolve só os valores."


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
