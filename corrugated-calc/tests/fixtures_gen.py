"""Генерира тестови ценоразписи във всички поддържани формати (тестови данни, не реални цени)."""

from __future__ import annotations

import csv
import io

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

HEADER_LINES = ["Ценоразпис велпапе - Тестов доставчик",
                "Цени в лв. за 1000 м2, без ДДС",
                "Валиден от 01.03.2026 до 31.12.2026",
                "Минимална заявка 1000 м2"]
TABLE = [["Код", "1000-1500 м2", "1500-2000 м2"],
         ["EC3008", "1060,00", "963,00"],
         ["EC4027", "1003,00", ""]]


def table_with(prices: dict[str, tuple[str, str]] | None = None) -> list[list[str]]:
    if not prices:
        return TABLE
    return [TABLE[0]] + [[code, a, b] for code, (a, b) in prices.items()]


def xlsx(table=TABLE, numeric=True) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Цени"
    for line in HEADER_LINES:
        ws.append([line])
    ws.append([])
    for i, row in enumerate(table):
        out = []
        for j, v in enumerate(row):
            if numeric and i > 0 and j > 0 and v:
                out.append(float(v.replace(",", ".")))
            else:
                out.append(v or None)
        ws.append(out)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xls(table=TABLE) -> bytes:
    import xlwt
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Ceni")
    r = 0
    for line in HEADER_LINES:
        ws.write(r, 0, line)
        r += 1
    r += 1
    for i, row in enumerate(table):
        for j, v in enumerate(row):
            if not v:
                continue
            ws.write(r, j, float(v.replace(",", ".")) if i > 0 and j > 0 else v)
        r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(table=TABLE, encoding="utf-8") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    for line in HEADER_LINES:
        w.writerow([line])
    for row in table:
        w.writerow(row)
    return buf.getvalue().encode(encoding)


def text_pdf(table=TABLE) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    pdfmetrics.registerFont(TTFont("DejaVu", FONT))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    st = ParagraphStyle("n", fontName="DejaVu", fontSize=11)
    story = [Paragraph(line, st) for line in HEADER_LINES] + [Spacer(1, 12)]
    t = Table(table, colWidths=[120, 120, 120])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "DejaVu"), ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


def image(table=TABLE, fmt="PNG", rotate_exif=False, header_lines=None) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(FONT, 34)
    img = Image.new("RGB", (1400, 760), "white")
    d = ImageDraw.Draw(img)
    y = 40
    for line in (HEADER_LINES if header_lines is None else header_lines):
        d.text((60, y), line, fill="black", font=font)
        y += 60
    y += 30
    xs = [60, 480, 900]
    for row in table:
        for x, v in zip(xs, row):
            d.text((x, y), v, fill="black", font=font)
        y += 70
    buf = io.BytesIO()
    if fmt == "HEIC":
        import pillow_heif
        pillow_heif.register_heif_opener()
    if rotate_exif:  # снимка, записана завъртяна с EXIF ориентация
        img = img.rotate(90, expand=True)
        exif = Image.Exif()
        exif[0x0112] = 6
        img.save(buf, format=fmt, exif=exif)
    else:
        img.save(buf, format="HEIF" if fmt == "HEIC" else fmt)
    return buf.getvalue()


def scanned_pdf(table=TABLE) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(image(table)))
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150)
    return buf.getvalue()
