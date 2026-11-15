from shared.risco import vol_anualizada


def resumo(fundo):
    return {"nome": fundo.nome, "vol": vol_anualizada(fundo.retornos)}
