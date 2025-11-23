# utils.py

from io import BytesIO

from pypdf import PdfReader


def extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    """
    Extracts text from a PDF given as raw bytes.
    Returns a single string with pages separated by blank lines.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    pages_text: list[str] = []
    for page in reader.pages:
        # extract_text() can be None on image-only pages
        txt = page.extract_text() or ""
        txt = txt.strip()
        if txt:
            pages_text.append(txt)
    return "\n\n".join(pages_text)