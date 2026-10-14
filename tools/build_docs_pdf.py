"""Build output/pdf/docs.pdf from docs.md for a version release.

Usage: python tools/build_docs_pdf.py [docs.md] [output/pdf/docs.pdf]

Supports the Markdown used by docs.md: headings with <a id> anchors, paragraphs,
bullet and numbered lists, tables, code blocks, quotes, **bold**, `code` and
links. Fails if an internal link has no anchor or a first table column would
wrap, as documents.md requires.
"""

import html
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

ROOT = Path(__file__).resolve().parents[1]
TITLE = "Лаборатория динамики и управления"
FONT_CANDIDATES = {
    "Body": ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    "Body-Bold": ("C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    "Body-Italic": ("C:/Windows/Fonts/ariali.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"),
    "Mono": ("C:/Windows/Fonts/consola.ttf", "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"),
}
LINK = "#1D4ED8"
MARGIN = 18 * mm
WIDTH = A4[0] - 2 * MARGIN


def register_fonts():
    for name, paths in FONT_CANDIDATES.items():
        path = next((p for p in paths if Path(p).is_file()), None)
        if path is None:
            raise SystemExit(f"Шрифт для {name} не найден: {', '.join(paths)}")
        pdfmetrics.registerFont(TTFont(name, path))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-Bold")


def styles():
    base = ParagraphStyle("body", fontName="Body", fontSize=9.5, leading=13, alignment=TA_LEFT, spaceAfter=5)
    return {
        "body": base,
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11, spaceAfter=0),
        "head": ParagraphStyle("head", parent=base, fontName="Body-Bold", fontSize=8.5, leading=11, spaceAfter=0),
        "h1": ParagraphStyle("h1", parent=base, keepWithNext=1, fontName="Body-Bold", fontSize=17, leading=21, spaceBefore=4, spaceAfter=8),
        "h2": ParagraphStyle("h2", parent=base, keepWithNext=1, fontName="Body-Bold", fontSize=13, leading=17, spaceBefore=12, spaceAfter=6,
                             textColor=colors.HexColor("#10243E")),
        "h3": ParagraphStyle("h3", parent=base, keepWithNext=1, fontName="Body-Bold", fontSize=11, leading=15, spaceBefore=8, spaceAfter=4),
        "quote": ParagraphStyle("quote", parent=base, fontName="Body-Italic", leftIndent=10, textColor=colors.HexColor("#344054")),
        "code": ParagraphStyle("code", fontName="Mono", fontSize=8.5, leading=11, leftIndent=8, spaceBefore=2, spaceAfter=8,
                               backColor=colors.HexColor("#F4F6F8"), borderPadding=5),
    }


def inline(text):
    """Convert the inline Markdown subset to ReportLab paragraph markup."""
    parts = re.split(r"(`[^`]*`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f'<font name="Mono">{html.escape(part[1:-1], quote=False)}</font>')
            continue
        part = html.escape(part, quote=False)
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)

        def link(match):
            label, target = match.group(1), match.group(2)
            href = target if not target.startswith("#") else target
            return f'<a href="{href}" color="{LINK}">{label}</a>'

        part = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, part)
        out.append(part)
    return "".join(out)


def plain_width(text, font, size, bold=False):
    """Width of one rendered line; **bold** segments use the bold face."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    width = 0.0
    for part in re.split(r"(`[^`]*`)", text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            width += pdfmetrics.stringWidth(part[1:-1], "Mono", size)
            continue
        for index, piece in enumerate(part.split("**")):
            face = "Body-Bold" if bold or index % 2 else font
            width += pdfmetrics.stringWidth(piece, face, size)
    return width


def build(source, target):
    register_fonts()
    st = styles()
    lines = Path(source).read_text(encoding="utf-8").split("\n")
    story, anchors, links, problems = [], set(), set(), []
    pending_anchor = None
    i = 0

    def para(text, style):
        nonlocal pending_anchor
        markup = inline(text)
        if pending_anchor:
            markup = f'<a name="{pending_anchor}"/>' + markup
            pending_anchor = None
        links.update(re.findall(r"\]\(#([^)]+)\)", text))
        return Paragraph(markup, st[style])

    while i < len(lines):
        line = lines[i]
        anchor = re.fullmatch(r'\s*<a id="([^"]+)"></a>\s*', line)
        if anchor:
            pending_anchor = anchor.group(1)
            anchors.add(pending_anchor)
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        heading = re.match(r"(#{1,3}) (.*)", line)
        if heading:
            story.append(para(heading.group(2), f"h{len(heading.group(1))}"))
            i += 1
            continue
        if line.startswith("```"):
            block = []
            i += 1
            while not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            story.append(Preformatted("\n".join(block), st["code"]))
            i += 1
            continue
        if line.startswith("> "):
            story.append(para(line[2:], "quote"))
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                row = lines[i].strip().strip("|").replace("\\|", "\0")
                cells = [c.strip().replace("\0", "|") for c in row.split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            flow = table(rows, st, links, problems)
            story.extend(flow if isinstance(flow, list) else [flow])
            continue
        bullet = re.match(r"(\s*)(- |\d+\. )(.*)", line)
        if bullet:
            items = []
            while i < len(lines):
                m = re.match(r"(\s*)(- |\d+\. )(.*)", lines[i])
                if not m:
                    break
                indent = len(m.group(1)) // 2
                items.append((indent, m.group(2).startswith("-"), m.group(3)))
                i += 1
            story.append(list_flowable(items, para))
            continue
        text = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,3} |```|> |\||\s*- |\s*\d+\. |\s*<a id=)", lines[i]):
            text.append(lines[i].strip())
            i += 1
        story.append(para(" ".join(text), "body"))

    missing = sorted(links - anchors)
    if missing:
        problems.append(f"Ссылки без якорей: {', '.join(missing)}")
    if problems:
        raise SystemExit("\n".join(problems))

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 8)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(MARGIN, A4[1] - 11 * mm, TITLE)
        canvas.drawRightString(A4[0] - MARGIN, 10 * mm, f"Страница {doc.page}")
        canvas.restoreState()

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    title = re.sub(r"^# ", "", next(l for l in lines if l.startswith("# ")))
    doc = SimpleDocTemplate(str(target), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=17 * mm,
                            bottomMargin=17 * mm, title=title, author="DynamicsControlLab")
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return len(anchors), len(links)


def list_flowable(items, para):
    flow, k = [], 0
    while k < len(items):
        indent, bulleted, text = items[k]
        children = []
        k += 1
        while k < len(items) and items[k][0] > indent:
            children.append(items[k])
            k += 1
        content = [para(text, "body")]
        if children:
            content.append(list_flowable([(i - indent - 1, b, t) for i, b, t in children], para))
        flow.append(ListItem(content, leftIndent=14))
    bulleted = items[0][1]
    return ListFlowable(flow, bulletType="bullet" if bulleted else "1", start="•" if bulleted else 1,
                        bulletFormat=None if bulleted else "%s.", bulletFontName="Body", bulletFontSize=9, leftIndent=14)


def table(rows, st, links, problems):
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    for row in rows:
        links.update(re.findall(r"\]\(#([^)]+)\)", "|".join(row)))
    pad = 12
    first = max(plain_width(r[0], "Body", 8.5, n == 0) for n, r in enumerate(rows)) + pad
    if columns == 1:
        widths = [WIDTH]
    else:
        first = min(first, WIDTH * 0.6)
        rest = WIDTH - first
        natural = [max(plain_width(r[c], "Body", 8.5, n == 0) for n, r in enumerate(rows)) + pad for c in range(1, columns)]
        total = sum(natural)
        widths = [first] + [rest * n / total for n in natural]
    for n, r in enumerate(rows):
        if plain_width(r[0], "Body", 8.5, n == 0) + pad > widths[0] + 0.01:
            problems.append(f"Первый столбец переносится: {r[0]}")
    data = [[Paragraph(inline(c), st["head"] if n == 0 else st["cell"]) for c in row] for n, row in enumerate(rows)]
    result = Table(data, colWidths=widths, repeatRows=1)
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF1F5")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D7DCE2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return KeepTogether([result, Spacer(1, 6)]) if len(rows) <= 4 else [result, Spacer(1, 6)]


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs.md"
    target = sys.argv[2] if len(sys.argv) > 2 else ROOT / "output/pdf/docs.pdf"
    anchor_count, link_count = build(source, target)
    print(f"{target}: якорей {anchor_count}, внутренних ссылок {link_count}")
