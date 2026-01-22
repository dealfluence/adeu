import sys
import traceback

from docx import Document


def debug_relationships(docx_path):
    print(f"Loading: {docx_path}")
    try:
        doc = Document(docx_path)
    except Exception as e:
        print(f"CRITICAL: Failed to load document: {e}")
        traceback.print_exc()
        return

    print("\n--- Document Relationships ---")
    rels = doc.part.rels

    # Standard Comments RelType
    COMMENTS_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"

    found_comments = False

    for rel_id, rel in rels.items():
        print(f"ID: {rel_id}")
        print(f"  Type: {rel.reltype}")

        try:
            # Attempt to access the target part.
            # This triggers the actual loading/parsing of the XML part.
            part = rel.target_part
            print(f"  Target Part: {part}")

            if hasattr(part, "partname"):
                print(f"  Partname: {part.partname}")

            if rel.reltype == COMMENTS_TYPE:
                found_comments = True
                print("  [!] FOUND COMMENTS PART")

        except Exception as e:
            print(f"  [X] ERROR loading target part: {e}")
            traceback.print_exc()

    if not found_comments:
        print("\n[!] WARNING: No relationship matching COMMENTS_TYPE found.")
    else:
        print("\n[OK] Comments relationship found.")


if __name__ == "__main__":
    target = r"C:\Users\mikko\workspace\docx-md-docx\test.docx"
    if len(sys.argv) > 1:
        target = sys.argv[1]
    debug_relationships(target)
