#!/usr/bin/env python3
"""Minimal Markdown -> PDF (headings, paragraphs, bullets, numbered lists, tables, bold, inline code) via headless Chrome.
Usage: md_to_pdf.py report.md [report.pdf]"""
import html, re, subprocess, sys, tempfile
from pathlib import Path

src = Path(sys.argv[1]); dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".pdf")
md = src.read_text(encoding="utf-8").split("\n")


def inl(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", t)


o, i, ul = [], 0, None
while i < len(md):
    l = md[i]
    if l.startswith("|"):
        rows = []
        while i < len(md) and md[i].startswith("|"):
            rows.append(md[i]); i += 1
        rows = [r for r in rows if not re.match(r"^\|[-| :]+\|$", r)]
        o.append("<table>")
        for k, r in enumerate(rows):
            tag = "th" if k == 0 else "td"
            o.append("<tr>" + "".join(f"<{tag}>{inl(c.strip())}</{tag}>" for c in r.strip().strip("|").split("|")) + "</tr>")
        o.append("</table>")
        continue
    m = re.match(r"^(- |\d+\. )", l)
    if m:
        kind = "ul" if m.group(1) == "- " else "ol"
        if ul != kind:
            if ul: o.append(f"</{ul}>")
            o.append(f"<{kind}>"); ul = kind
        o.append("<li>" + inl(l[len(m.group(1)):]) + "</li>")
    else:
        if ul: o.append(f"</{ul}>"); ul = None
        if l.startswith("### "): o.append("<h3>" + inl(l[4:]) + "</h3>")
        elif l.startswith("## "): o.append("<h2>" + inl(l[3:]) + "</h2>")
        elif l.startswith("# "): o.append("<h1>" + inl(l[2:]) + "</h1>")
        elif l.strip(): o.append("<p>" + inl(l) + "</p>")
    i += 1
if ul: o.append(f"</{ul}>")
css = ('@page{size:A4;margin:14mm}body{font:11.5px/1.5 "DejaVu Sans",sans-serif;color:#1a1a1a}'
       'h1{font-size:18px;color:#0b3d6b}h2{font-size:14px;color:#0b3d6b;border-bottom:1px solid #ccd;padding-bottom:3px;margin-top:16px}'
       'h3{font-size:12.5px;color:#0b3d6b}table{border-collapse:collapse;width:100%;margin:8px 0;font-size:9.8px;page-break-inside:avoid}'
       'th{background:#0b3d6b;color:#fff;padding:4px;text-align:left}td{border:1px solid #ccd;padding:4px;vertical-align:top}'
       'code{background:#e6ebf5;color:#1a1a1a;padding:0 3px;border-radius:2px}'
       'h1 code,h2 code,h3 code{background:transparent;color:inherit;padding:0}'
       'th code{background:transparent;color:#fff;font-weight:bold;padding:0}li{margin:3px 0}')
tmp = Path(tempfile.mkdtemp()) / "r.html"
tmp.write_text(f"<!doctype html><meta charset=utf-8><style>{css}</style>" + "\n".join(o), encoding="utf-8")
subprocess.run(["google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={dst}", str(tmp)],
               capture_output=True, timeout=120)
print("wrote", dst)
