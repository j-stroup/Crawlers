"""Generate the Open Graph / social-share card for v0rt (site/assets/og.png).

A one-time (re-runnable) build helper - NOT part of the deployed site.
Renders the terminal look at 1200x630 using Consolas (falls back to the
Pillow default font if it isn't present).

    python make_og_card.py
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "site", "assets", "og.png")

W, H = 1200, 630
BG      = (4, 8, 4)
GREEN   = (0, 255, 102)
GREEN2  = (25, 255, 122)
DIM     = (11, 122, 58)
CYAN    = (59, 247, 228)
MAGENTA = (255, 95, 210)
INK     = (198, 255, 217)
SCAN    = (7, 16, 9)


def font(bold, size):
    names = (["consolab.ttf", "Consolas Bold"] if bold else ["consola.ttf"])
    for n in names + ["cour.ttf", "DejaVuSansMono.ttf"]:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def centered(draw, y, text, fnt, fill, glitch=False):
    w = draw.textlength(text, font=fnt)
    x = (W - w) / 2
    if glitch:
        draw.text((x - 4, y), text, font=fnt, fill=CYAN)
        draw.text((x + 4, y), text, font=fnt, fill=MAGENTA)
    draw.text((x, y), text, font=fnt, fill=fill)
    return x


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# subtle CRT scanlines
for y in range(0, H, 3):
    d.line([(0, y), (W, y)], fill=SCAN, width=1)

# terminal frame + title bar
d.rectangle([30, 30, W - 30, H - 30], outline=DIM, width=2)
d.rectangle([30, 30, W - 30, 78], fill=(6, 26, 14), outline=DIM, width=2)
for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
    d.ellipse([54 + i * 26, 46, 70 + i * 26, 62], fill=c)
d.text((150, 46), "root@v0rt: ~/finds", font=font(False, 22), fill=DIM)

# prompt + wordmark + taglines
d.text((70, 150), "visitor@v0rt:~$ ./v0rt --show-loot", font=font(False, 30), fill=GREEN2)
centered(d, 215, "v0rt.com", font(True, 150), GREEN, glitch=True)
centered(d, 410, "human things hiding in the source of the open web", font(False, 36), INK)
centered(d, 470, "jokes  ·  confessions  ·  ASCII art  ·  console eggs  ·  humans.txt",
         font(False, 26), DIM)
d.text((70, H - 64), "// collected gently from the open web, one page at a time",
       font=font(False, 22), fill=DIM)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT, "PNG", optimize=True)
print("wrote", OUT, "(%d bytes)" % os.path.getsize(OUT))
