"""Pares rotulados à mão para medir o ``near``: ``real`` = duplicata que alguém deveria reaproveitar,
``ruido`` = parecida só no formato,
``real_dificil`` = duplicata que o near hoje não separa do ruído (xfail; vira ``real`` quando passar). Ao ajustar limiar/pesos, rode ``pytest tests/test_near_labeled.py -s``
para ver precisão/recall; um par que escapou em produção entra aqui como caso novo.
"""

CASES = [
    ("real", "dia util com um feriado a mais", '''
def proximo_dia_util(data, feriados):
    atual = data + timedelta(days=1)
    while atual.weekday() >= 5 or atual in feriados:
        atual = atual + timedelta(days=1)
    if atual.year != data.year:
        log.info("virou o ano em %s", atual)
    return atual
''', '''
def next_business_day(d, holidays, extra=()):
    cur = d + timedelta(days=1)
    while cur.weekday() >= 5 or cur in holidays or cur in extra:
        cur = cur + timedelta(days=1)
    if cur.year != d.year:
        log.info("virou o ano em %s", cur)
    return cur
'''),
    ("real", "parser br com remocao de simbolo", '''
def parse_valor_br(texto):
    if texto is None:
        return None
    limpo = texto.strip().replace("R$", "").replace(".", "").replace(",", ".")
    if not limpo:
        return None
    sinal = -1 if limpo.startswith("(") and limpo.endswith(")") else 1
    limpo = limpo.strip("()")
    try:
        return sinal * float(limpo)
    except ValueError:
        return None
''', '''
def to_float_br(s):
    if s is None:
        return None
    t = s.strip().replace("R$", "").replace("US$", "").replace(".", "").replace(",", ".")
    if not t:
        return None
    neg = -1 if t.startswith("(") and t.endswith(")") else 1
    t = t.strip("()")
    try:
        return neg * float(t)
    except ValueError:
        return None
'''),
    ("real_dificil", "retorno acumulado com filtro extra", '''
def retorno_acumulado(serie, inicio, fim):
    janela = serie[(serie.index >= inicio) & (serie.index <= fim)]
    if janela.empty:
        return None
    base = janela.iloc[0]
    if base == 0:
        return None
    ultimo = janela.iloc[-1]
    return ultimo / base - 1
''', '''
def retorno_periodo(s, ini, fim, minimo=2):
    jan = s[(s.index >= ini) & (s.index <= fim)]
    if jan.empty or len(jan) < minimo:
        return None
    base = jan.iloc[0]
    if base == 0:
        return None
    ult = jan.iloc[-1]
    return ult / base - 1
'''),
    ("ruido", "dois CRUDs de tabelas diferentes", '''
def salvar_cliente(session, dados):
    obj = session.get(Cliente, dados["id"])
    if obj is None:
        obj = Cliente(id=dados["id"])
        session.add(obj)
    obj.nome = dados["nome"]
    obj.email = dados["email"]
    session.commit()
    session.refresh(obj)
    return obj
''', '''
def salvar_fundo(session, dados):
    obj = session.get(Fundo, dados["id"])
    if obj is None:
        obj = Fundo(id=dados["id"])
        session.add(obj)
    obj.cnpj = dados["cnpj"]
    obj.gestor = dados["gestor"]
    session.commit()
    session.refresh(obj)
    return obj
'''),
    ("ruido", "dois endpoints com o mesmo esqueleto", '''
@router.get("/clientes/{cid}")
def get_cliente(cid: int, session=Depends(get_session)):
    row = session.get(Cliente, cid)
    if row is None:
        raise HTTPException(status_code=404, detail="nao encontrado")
    return {"id": row.id, "nome": row.nome}
''', '''
@router.get("/fundos/{fid}")
def get_fundo(fid: int, session=Depends(get_session)):
    row = session.get(Fundo, fid)
    if row is None:
        raise HTTPException(status_code=404, detail="nao encontrado")
    return {"id": row.id, "cnpj": row.cnpj}
'''),
    ("ruido", "mesma assinatura de leitura, conteudo diferente", '''
def ler_planilha_posicoes(caminho):
    df = pd.read_excel(caminho, sheet_name="Posicoes", header=2)
    df = df.rename(columns={"Ativo": "ativo", "Quantidade": "qtd", "Valor": "valor"})
    df = df.dropna(subset=["ativo"])
    df["valor"] = df["valor"].astype(float)
    return df
''', '''
def ler_planilha_taxas(caminho):
    df = pd.read_excel(caminho, sheet_name="Taxas", header=0)
    df = df.rename(columns={"Fundo": "fundo", "Adm": "adm", "Perf": "perf"})
    df = df.dropna(subset=["fundo"])
    df["perf"] = df["perf"].astype(float) / 100
    return df
'''),
]
