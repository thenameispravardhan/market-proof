"""Plain-text report -> PDF. Monospace throughout so ASCII tables keep their alignment.

Usage:  python scripts/txt2pdf.py COMPETITIVE_EDGE.txt [out.pdf]
"""
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Preformatted, Spacer

FONT_SIZE = 8  # 108-char lines fit A4 at 8pt Courier with 32pt margins

body = ParagraphStyle("body", fontName="Courier", fontSize=FONT_SIZE, leading=FONT_SIZE * 1.2)
head = ParagraphStyle("head", parent=body, fontName="Courier-Bold", fontSize=9.5,
                      leading=11.4, textColor=colors.HexColor("#0b3d5c"))


def is_rule(s):
    """A run of = or - used to underline a heading."""
    s = s.strip()
    return len(s) > 3 and (set(s) <= {"="} or set(s) <= {"-"})


def build(src: Path, out: Path):
    lines = src.read_text(encoding="utf-8").splitlines()
    story, buf, i = [], [], 0

    def flush():
        if buf:
            story.append(Preformatted("\n".join(buf), body))
            buf.clear()

    while i < len(lines):
        ln, nxt = lines[i], lines[i + 1] if i + 1 < len(lines) else ""
        if ln.strip() and is_rule(nxt) and not is_rule(ln):
            flush()
            story.append(Spacer(1, 6))
            story.append(Preformatted(ln + "\n" + nxt, head))
            i += 2
        else:
            buf.append(ln)
            i += 1
    flush()

    stamp = src.name
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Courier", 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(32, 20, stamp)
        canvas.drawRightString(A4[0] - 32, 20, "page %d" % doc.page)
        canvas.restoreState()

    doc = BaseDocTemplate(str(out), pagesize=A4, leftMargin=32, rightMargin=32,
                          topMargin=34, bottomMargin=34, title=src.stem.replace("_", " "))
    doc.addPageTemplates([PageTemplate(
        id="p", frames=[Frame(32, 34, A4[0] - 64, A4[1] - 68, id="f")], onPage=footer)])
    doc.build(story)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".pdf")
    # A line wider than the frame would silently run off the page — catch it early.
    widest = max(len(l.rstrip()) for l in src.read_text(encoding="utf-8").splitlines())
    limit = int((A4[0] - 64) / (FONT_SIZE * 0.6))
    assert widest <= limit, f"{src.name}: longest line {widest} chars > {limit} that fit at {FONT_SIZE}pt"
    print("wrote", build(src, out))
