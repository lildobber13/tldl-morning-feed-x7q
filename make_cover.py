"""Generates docs/cover.png (1400x1400) for the podcast. Run once."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 1400
BG, FG, ACCENT = (17, 24, 39), (245, 245, 240), (251, 191, 36)
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size=size)


img = Image.new("RGB", (SIZE, SIZE), BG)
d = ImageDraw.Draw(img)


def centered(text, y, f, fill):
    w = d.textlength(text, font=f)
    d.text(((SIZE - w) / 2, y), text, font=f, fill=fill)


centered("TLDL", 430, font(BOLD, 420), FG)
d.rectangle([(350, 940), (SIZE - 350, 952)], fill=ACCENT)
centered("Too Long, Didn't Listen", 1000, font(REG, 78), FG)
centered("YOUR MORNING BRIEFING", 1120, font(BOLD, 44), ACCENT)

out = Path(__file__).parent / "docs" / "cover.png"
out.parent.mkdir(exist_ok=True)
img.save(out)
print("wrote", out)
