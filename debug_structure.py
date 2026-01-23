import argparse
import sys
import zipfile
from xml.dom.minidom import parseString


def pretty_print_xml(xml_bytes):
    try:
        dom = parseString(xml_bytes)
        return dom.toprettyxml(indent="  ")
    except Exception:
        return xml_bytes.decode("utf-8", errors="ignore")


def inspect_docx(path: str):
    print(f"=== Inspecting: {path} ===")
    
    try:
        with zipfile.ZipFile(path, "r") as z:
            all_files = z.namelist()
            
            # 1. Inspect Content Types
            if "[Content_Types].xml" in all_files:
                print("\n[Content Types]")
                print(pretty_print_xml(z.read("[Content_Types].xml")))

            # 2. Inspect Relationships
            if "word/_rels/document.xml.rels" in all_files:
                print("\n[Document Relationships]")
                print(pretty_print_xml(z.read("word/_rels/document.xml.rels")))

            # 3. Find and Dump Comment Parts
            print("\n[Comment Parts]")
            comment_files = [f for f in all_files if "comments" in f.lower() and f.endswith(".xml")]
            
            if not comment_files:
                print("  No comment parts found.")
            
            for fname in comment_files:
                print(f"\n  --- Part: {fname} ---")
                content = z.read(fname)
                print(pretty_print_xml(content))

    except Exception as e:
        print(f"Error inspecting file: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deep inspection of DOCX structure for comment debugging.")
    parser.add_argument("file", help="Path to DOCX file")
    args = parser.parse_args()
    
    inspect_docx(args.file)