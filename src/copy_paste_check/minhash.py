"""MinHash + LSH em Python puro, para achar funções parecidas (clone "Tipo 3").

Cada função vira um conjunto de "shingles" (janelas de K tokens). O MinHash
resume o conjunto em NUM_PERM inteiros; a fração de posições iguais entre duas
assinaturas estima a similaridade de Jaccard. O LSH divide a assinatura em
BANDS faixas para buscar candidatos no índice sem comparar com tudo.
"""

from __future__ import annotations

import array
import hashlib
import zlib

K = 5
NUM_PERM = 64
BANDS = 16
ROWS = NUM_PERM // BANDS


def _shingles(tokens: list[str]) -> set[str]:
    if len(tokens) < K:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + K]) for i in range(len(tokens) - K + 1)}


def shingle_ids(tokens: list[str]) -> list[int]:
    """Ids estáveis (crc32) dos shingles distintos, para o Jaccard ponderado por raridade."""
    return sorted({zlib.crc32(g.encode()) for g in _shingles(tokens)})


def signature(tokens: list[str]) -> list[int]:
    # Um único shake_128 por shingle gera os NUM_PERM hashes de 32 bits de uma vez
    # (equivale a NUM_PERM funções de hash independentes); o min por posição é feito em C.
    rows = [memoryview(hashlib.shake_128(g.encode()).digest(NUM_PERM * 4)).cast("I") for g in _shingles(tokens)]
    return list(map(min, zip(*rows)))


def to_bytes(sig: list[int]) -> bytes:
    return array.array("I", sig).tobytes()


def from_bytes(blob: bytes) -> list[int]:
    arr = array.array("I")
    arr.frombytes(blob)
    return arr.tolist()


def bands(sig: list[int]) -> list[str]:
    return [
        f"{i}:" + hashlib.blake2b(to_bytes(sig[i * ROWS : (i + 1) * ROWS]), digest_size=8).hexdigest()
        for i in range(BANDS)
    ]


def similarity(a: list[int], b: list[int]) -> float:
    return sum(x == y for x, y in zip(a, b)) / NUM_PERM
