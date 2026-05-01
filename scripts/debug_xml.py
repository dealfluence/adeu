import sys

from docx import Document

# Usage: python debug_xml.py path/to/file.docx "search string"


def main():
    if len(sys.argv) < 3:
        print("Usage: python debug_xml.py <docx_file> <search_text>")
        sys.exit(1)

    path = sys.argv[1]
    search = sys.argv[2]

    try:
        doc = Document(path)
    except Exception as e:
        print(f"Error loading document: {e}")
        sys.exit(1)

    print(f"Scanning '{path}' for paragraphs containing '{search}'...\n")

    found = False
    for i, p in enumerate(doc.paragraphs):
        # We search the raw XML because p.text often hides deletions/revisions
        xml = p._element.xml
        if search in xml:
            found = True
            print(f"=== Paragraph {i} Match ===")
            print(xml)  # This will print the full OOXML structure
            print("===========================\n")

    if not found:
        print(f"Text '{search}' not found in the document body XML.")


if __name__ == "__main__":
    main()
