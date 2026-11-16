"""Execução de funções de texto: mesma saída = ``execucao``; vazio x None = só pista (``execucao~``)."""

import sys

from copy_paste_check import execfp

BASE = '''
def so_digitos(v):
    return re.sub("[^0-9]", "", v or "")


def limpa(v):
    return (v or "").strip()
'''

NOVO = '''
def _cnpj_limpo(cnpj):
    return "".join(c for c in (cnpj or "") if c.isdigit()).strip()


def _texto(v):
    return v.strip() or None
'''


def test_execucao_texto(tmp_path):
    (tmp_path / "base.py").write_text(BASE, encoding="utf-8")
    got = execfp.matches(tmp_path, ["base.py"], {"novo.py": NOVO}, python=sys.executable)
    kinds = {(k[1], m.qualname): m.kind for k, ms in got.items() for m in ms}
    assert kinds[("_cnpj_limpo", "so_digitos")] == "execucao"
    assert kinds[("_texto", "limpa")] == "execucao~"  # "" vira None numa e "" na outra
    assert ("_cnpj_limpo", "limpa") not in kinds
