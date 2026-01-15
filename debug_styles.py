import sys

from docx import Document

# Usage: python debug_styles.py path/to/doc.docx
try:
    path = sys.argv[1]
    doc = Document(path)
    print(f"--- Styles used in {path} ---")
    seen = set()
    for p in doc.paragraphs:
        if p.style.name not in seen:
            print(f"Style: '{p.style.name}' | Text sample: '{p.text[:20]}...'")
            seen.add(p.style.name)
except Exception as e:
    print(e)
