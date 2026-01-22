import sys
import zipfile
import re
from xml.dom.minidom import parseString

def pretty_print_xml(xml_str):
    try:
        dom = parseString(xml_str)
        return dom.toprettyxml(indent="  ")
    except Exception:
        return xml_str

def inspect(path):
    print(f"Inspecting: {path}")
    try:
        with zipfile.ZipFile(path, 'r') as z:
            # Find comments XML
            files = [f for f in z.namelist() if f.startswith('word/comments') and f.endswith('.xml')]
            if not files:
                print("No comments XML found.")
                return

            for fname in files:
                print(f"\n=== {fname} ===")
                data = z.read(fname).decode('utf-8')
                formatted = pretty_print_xml(data)
                print(formatted)
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # path = r"C:\Users\mikko\workspace\docx-md-docx\test.docx"
    # Using the path provided in previous prompt contexts or command line
    target = sys.argv[1] if len(sys.argv) > 1 else "test.docx"
    inspect(target)