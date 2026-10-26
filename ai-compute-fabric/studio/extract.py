"""Turn an uploaded file into plain text, refusing anything suspicious. Parsing is delegated to agents/doc_executors.py,
which already enforces page, size, zip-bomb and path limits; this module adds type detection by content, not by name."""
from __future__ import annotations

import tempfile
from pathlib import Path

from agents.doc_executors import DocumentExecutor, RefusedInput
from studio.errors import bad

MAX_TEXT_CHARS = 1_000_000          # indexing is synchronous and measured at ~26 s per million characters; keeps one request under a proxy's 60 s
EXT_MIME = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown", ".html": "text/html", ".htm": "text/html",
            ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def detect(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    mime = EXT_MIME.get(ext)
    if mime is None:
        raise bad(f"file type {ext or '(none)'} is not supported; use .txt, .md, .html, .pdf or .docx", "unsupported_type")
    if mime == "application/pdf" and not data.startswith(b"%PDF"):
        raise bad("the file is named .pdf but is not a PDF", "type_mismatch")
    if mime.endswith("wordprocessingml.document") and not data.startswith(b"PK"):
        raise bad("the file is named .docx but is not a Word document", "type_mismatch")
    if mime.startswith("text/") and b"\x00" in data[:4096]:
        raise bad("the file looks binary, not text", "type_mismatch")
    return mime


def extract_text(name: str, data: bytes) -> str:
    mime = detect(name, data)
    with tempfile.TemporaryDirectory(prefix="studio-upload-") as d:
        (Path(d) / "upload").write_bytes(data)
        try:
            parsed = DocumentExecutor(d).parse("upload", mime)
        except RefusedInput as e:
            raise bad(f"the document was refused: {e}", "refused_document") from None
        except Exception as e:      # a malformed file must be a 400, not a crash of the service
            raise bad(f"the document could not be read ({type(e).__name__})", "unreadable_document") from None
    text = "\n\n".join(p for p in parsed["pages"] if p).strip()
    for table in parsed.get("tables", []):
        text += "\n" + "\n".join(" | ".join(row) for row in table)
    text = text.strip()
    if not text:
        raise bad("no text could be extracted (a scanned PDF needs OCR first)", "empty_document")
    if len(text) > MAX_TEXT_CHARS:
        raise bad(f"the document has more than {MAX_TEXT_CHARS:,} characters of text (indexing takes about 26 s per million); split it into several files", "too_large")
    return text
