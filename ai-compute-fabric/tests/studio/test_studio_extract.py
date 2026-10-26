"""Real PDF / DOCX files (built here with reportlab and python-docx) and hostile ones through the Studio's text extraction."""
import io
import zipfile
from pathlib import Path

import pytest

from studio.errors import ApiError
from studio.extract import MAX_TEXT_CHARS, extract_text

docx = pytest.importorskip("docx")
rl = pytest.importorskip("reportlab.pdfgen.canvas")
FONTS = [p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf") if Path(p).exists()]


def make_docx(paragraphs, table=None) -> bytes:
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, v in enumerate(row):
                t.cell(i, j).text = v
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def make_pdf(lines, pages=1) -> bytes:
    b = io.BytesIO()
    c = rl.Canvas(b)
    if FONTS:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont("DV", FONTS[0]))
        c.setFont("DV", 12)
    for _ in range(pages):
        for i, l in enumerate(lines):
            c.drawString(50, 800 - 20 * i, l)
        c.showPage()
    c.save()
    return b.getvalue()


def refused(name, data, code=None):
    with pytest.raises(ApiError) as e:
        extract_text(name, data)
    assert e.value.status == 400 and (code is None or e.value.code == code), (e.value.code, e.value.message)


def test_real_docx_with_vietnamese_text_and_a_table():
    t = extract_text("quy-dinh.docx", make_docx(["Nhân viên chính thức được nghỉ phép năm 12 ngày.", "Đơn nộp trước 3 ngày."], [["Loại", "Số ngày"], ["Phép năm", "12"]]))
    assert "nghỉ phép năm 12 ngày" in t and "Phép năm | 12" in t


@pytest.mark.skipif(not FONTS, reason="no TTF font with Vietnamese glyphs on this machine")
def test_real_pdf_with_vietnamese_text_two_pages():
    t = extract_text("bao-cao.pdf", make_pdf(["Báo cáo doanh thu quý ba tăng 12 phần trăm."], pages=2))
    assert "doanh thu quý ba" in t


def test_pdf_without_text_is_refused_with_an_ocr_hint():
    b = io.BytesIO()
    c = rl.Canvas(b)
    c.rect(10, 10, 100, 100)
    c.showPage()
    c.save()
    refused("scan.pdf", b.getvalue(), "empty_document")


def test_type_confusion_and_corrupt_files_are_400_not_crashes():
    refused("a.pdf", b"hello world, not a pdf", "type_mismatch")
    refused("a.docx", b"hello", "type_mismatch")
    refused("a.txt", b"abc\x00\x01\x02" * 10, "type_mismatch")
    refused("a.exe", b"MZ....", "unsupported_type")
    refused("noext", b"text", "unsupported_type")
    refused("a.pdf", b"%PDF-1.4\n" + b"garbage" * 50)
    refused("a.docx", b"PK\x03\x04" + b"garbage" * 50)
    good = make_docx(["xin chào"])
    refused("a.docx", good[: len(good) // 2])                        # truncated zip


def test_zip_bomb_docx_is_refused():
    base = zipfile.ZipFile(io.BytesIO(make_docx(["x"])))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n in base.namelist():
            z.writestr(n, base.read(n))
        z.writestr("word/media/bomb.bin", b"\0" * (400 * 1024 * 1024))     # 400 MiB of zeros -> a few hundred KiB compressed
    assert len(out.getvalue()) < 2_000_000
    refused("bomb.docx", out.getvalue(), "refused_document")


def test_text_over_the_character_limit_is_refused():
    refused("big.txt", ("Một câu dài. " * 500_000).encode(), "too_large")
    assert MAX_TEXT_CHARS == 1_000_000


def test_html_scripts_do_not_leak_into_text():
    t = extract_text("p.html", "<html><body><script>alert('XSS-SECRET')</script><p>Nội dung chính</p></body></html>".encode())
    assert "Nội dung chính" in t and "XSS-SECRET" not in t
