import numpy as np


def vol_anualizada(retornos, dias_uteis=252):
    """Volatilidade anualizada de uma série de retornos diários."""
    validos = retornos.dropna()
    if len(validos) < 2:
        return float("nan")
    return float(validos.std(ddof=1) * np.sqrt(dias_uteis))
