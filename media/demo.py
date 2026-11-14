"""Gera os GIFs do README a partir de examples/vol, checados pelo hook de verdade.

    uv run --with pillow python media/demo.py

Duas versões de examples/vol/relatorio.py para o pedido "põe a vol dos últimos 12
meses no resumo": a primeira tentativa do agente (recria a conta) e a segunda
(reusa ``vol_anualizada``). Cada uma passa pelo ``check_change`` do hook (o script
confere o veredito esperado) e o texto do bloqueio no GIF é o que o hook devolveu.
Precisa do Pillow (não é dependência do copy-paste-check).
"""
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from copy_paste_check.hook import check_change  # noqa: E402

COPIA = '''import numpy as np
from shared.risco import vol_anualizada


def resumo(fundo):
    return {"nome": fundo.nome,
            "vol": vol_anualizada(fundo.retornos),
            "vol_12m": volatilidade(fundo.retornos.tail(252))}


def volatilidade(serie, n=252):
    r = serie.dropna()
    if len(r) < 2:
        return float("nan")
    return float(r.std(ddof=1) * np.sqrt(n))
'''
REUSO = '''from shared.risco import vol_anualizada


def resumo(fundo):
    return {"nome": fundo.nome,
            "vol": vol_anualizada(fundo.retornos),
            "vol_12m": vol_anualizada(fundo.retornos.tail(252))}
'''

W, PAD, LH = 640, 16, 18
BG, FG, DIM = "#1e1e2e", "#cdd6f4", "#7f849c"
RED, GREEN, YELLOW = "#f38ba8", "#a6e3a1", "#f9e2af"


def font(size):
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "Menlo.ttc", "LiberationMono-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


MONO = font(14)


def hook(project, content):
    path = project / "relatorio.py"
    inp = {"file_path": str(path), "content": content}
    return check_change("PreToolUse", "Write", inp, mode="block")


def frame(title, code_lines, box=None):
    """box = (cor, linhas) desenhado abaixo do código."""
    box_h = (len(box[1]) * LH + 2 * PAD) if box else 0
    h = PAD * 3 + LH * (len(code_lines) + 1) + box_h
    img = Image.new("RGB", (W, max(h, 380)), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD, PAD), title, fill=DIM, font=MONO)
    y = PAD + LH * 2
    for line in code_lines:
        d.text((PAD, y), line, fill=FG, font=MONO)
        y += LH
    if box:
        color, lines = box
        y += PAD // 2
        d.rectangle((PAD, y, W - PAD, y + box_h - PAD), outline=color, width=2)
        for i, line in enumerate(lines):
            d.text((PAD * 2, y + PAD // 2 + i * LH), line, fill=color, font=MONO)
    return img


def typing(title, code, box, hold=40):
    lines = code.rstrip("\n").splitlines()
    frames = [frame(title, lines[:i]) for i in range(len(lines) + 1)]
    frames += [frame(title, lines, box)] * hold
    return frames


def save(name, frames):
    h = max(f.height for f in frames)
    frames = [f if f.height == h else _pad(f, h) for f in frames]
    frames[0].save(ROOT / "media" / f"{name}.gif", save_all=True, append_images=frames[1:],
                   duration=90, loop=0, optimize=True)


def _pad(img, h):
    out = Image.new("RGB", (img.width, h), BG)
    out.paste(img, (0, 0))
    return out


def wrap(text, width=72):
    out = []
    for raw in text.splitlines():
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            cut = cut if cut > 0 else width
            out.append(raw[:cut])
            raw = "  " + raw[cut:].lstrip()
        out.append(raw)
    return out


def main():
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "vol"
        shutil.copytree(ROOT / "examples" / "vol", project)
        (project / ".cpcheck").mkdir()  # marca a raiz do repo para o índice

        bloqueio = hook(project, COPIA)
        assert bloqueio and bloqueio[0] == "block", bloqueio
        assert hook(project, REUSO) is None

    title = "Claude, pedido: vol dos últimos 12 meses"
    motivo = " ".join(bloqueio[1]).replace("`", "")
    save("vol_sem", typing(title, COPIA, (YELLOW, ["sem cpcheck: merged.", "Agora são duas vols no repo."])))
    save("vol_bloqueio", typing(title, COPIA, (RED, ["cpcheck (PreToolUse): escrita negada", *wrap(motivo)])))
    save("vol_reuso", typing(title, REUSO, (GREEN, ["cpcheck: nada a dizer. Reusou vol_anualizada."])))
    print("ok:", motivo)


if __name__ == "__main__":
    main()
