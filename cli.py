import sys
import argparse
from pathlib import Path
from io import BytesIO

from adeu.ingest import extract_text_from_stream
from adeu.redline.engine import RedlineEngine
from adeu.diff import generate_edits_from_text

def get_original_text(docx_path: Path) -> str:
    with open(docx_path, "rb") as f:
        content = f.read()
        stream = BytesIO(content)
        stream.name = docx_path.name
        return extract_text_from_stream(stream, filename=docx_path.name)

def mode_extract(docx_path: Path):
    print(f"📄 Extracting text from: {docx_path}")
    text = get_original_text(docx_path)
    
    output_md = docx_path.with_suffix(".md")
    with open(output_md, "w", encoding="utf-8") as f:
        f.write(text)
    
    print(f"✅ Saved to: {output_md}")

def mode_redline(docx_path: Path, md_path: Path):
    print(f"🔄 Redlining '{docx_path}' using changes from '{md_path}'...")
    
    # 1. Get Original Text
    original_text = get_original_text(docx_path)
    
    # 2. Get Modified Text
    if not md_path.exists():
        print(f"❌ Error: Markdown file {md_path} not found.")
        sys.exit(1)
        
    with open(md_path, "r", encoding="utf-8") as f:
        modified_text = f.read()
        
    # 3. Compute Diffs
    print("🔍 Computing differences...")
    edits = generate_edits_from_text(original_text, modified_text)
    print(f"   Found {len(edits)} changes.")
    
    # 4. Apply Edits
    print("✏️  Applying redlines...")
    with open(docx_path, "rb") as f:
        stream = BytesIO(f.read())
        
    engine = RedlineEngine(stream)
    engine.apply_edits(edits)
    
    # 5. Save
    output_docx = docx_path.with_name(f"{docx_path.stem}_redlined.docx")
    result_stream = engine.save_to_stream()
    
    with open(output_docx, "wb") as f:
        f.write(result_stream.getvalue())
        
    print(f"✅ Success! Redlined file saved to: {output_docx}")

def main():
    parser = argparse.ArgumentParser(description="Adeu: DOCX <-> Markdown Workflow")
    parser.add_argument("docx_file", type=Path, help="Path to input .docx file")
    parser.add_argument("md_file", type=Path, nargs="?", help="Path to modified .md file (Optional)")
    
    args = parser.parse_args()
    
    if not args.docx_file.exists():
        print(f"Error: File {args.docx_file} not found.")
        sys.exit(1)
        
    if args.md_file:
        # Mode 2: Redline
        mode_redline(args.docx_file, args.md_file)
    else:
        # Mode 1: Extract
        mode_extract(args.docx_file)

if __name__ == "__main__":
    main()