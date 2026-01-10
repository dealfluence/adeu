import sys
import json
import argparse
from pathlib import Path
from io import BytesIO
from typing import List

from adeu.ingest import extract_text_from_stream
from adeu.redline.engine import RedlineEngine
from adeu.diff import generate_edits_from_text
from adeu.models import DocumentEdit, EditOperationType

def get_original_text(docx_path: Path) -> str:
    with open(docx_path, "rb") as f:
        content = f.read()
        stream = BytesIO(content)
        stream.name = docx_path.name
        return extract_text_from_stream(stream, filename=docx_path.name)

def load_edits_from_json(json_path: Path) -> List[DocumentEdit]:
    """
    Parses a JSON file with structure:
    [
        {"original": "Text", "replace": "New Text", "comment": "Reasoning"},
        ...
    ]
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    edits = []
    for item in data:
        original = item.get("original", "")
        replace = item.get("replace", "")
        comment = item.get("comment", None)
        
        # Determine Operation Type
        if original and replace:
            op = EditOperationType.MODIFICATION
        elif original and not replace:
            op = EditOperationType.DELETION
        elif not original and replace:
            op = EditOperationType.INSERTION
        else:
            continue # Skip empty
            
        edits.append(DocumentEdit(
            operation=op,
            target_text=original,
            new_text=replace,
            comment=comment
        ))
    return edits

def mode_extract(docx_path: Path):
    print(f"📄 Extracting text from: {docx_path}")
    text = get_original_text(docx_path)
    
    output_md = docx_path.with_suffix(".md")
    with open(output_md, "w", encoding="utf-8") as f:
        f.write(text)
    
    print(f"✅ Saved to: {output_md}")

def mode_redline(docx_path: Path, changes_path: Path):
    print(f"🔄 Redlining '{docx_path}'...")
    
    edits = []
    
    # 1. Determine Input Type (JSON vs Markdown)
    if changes_path.suffix.lower() == ".json":
        print(f"   📂 Loading structured changes from {changes_path}")
        edits = load_edits_from_json(changes_path)
    else:
        # Markdown Diff Mode
        print(f"   📝 Calculating diffs from {changes_path}")
        original_text = get_original_text(docx_path)
        with open(changes_path, "r", encoding="utf-8") as f:
            modified_text = f.read()
        edits = generate_edits_from_text(original_text, modified_text)

    print(f"   Found {len(edits)} edits to apply.")
    
    # 2. Apply Edits
    print("✏️  Applying redlines...")
    with open(docx_path, "rb") as f:
        stream = BytesIO(f.read())
        
    engine = RedlineEngine(stream)
    engine.apply_edits(edits)
    
    # 3. Save
    output_docx = docx_path.with_name(f"{docx_path.stem}_redlined.docx")
    result_stream = engine.save_to_stream()
    
    with open(output_docx, "wb") as f:
        f.write(result_stream.getvalue())
        
    print(f"✅ Success! Redlined file saved to: {output_docx}")

def main():
    parser = argparse.ArgumentParser(description="Adeu: DOCX Redlining Engine")
    parser.add_argument("docx_file", type=Path, help="Path to input .docx file")
    parser.add_argument("changes_file", type=Path, nargs="?", help="Path to modified .md OR .json file")
    
    args = parser.parse_args()
    
    if not args.docx_file.exists():
        print(f"Error: File {args.docx_file} not found.")
        sys.exit(1)
        
    if args.changes_file:
        mode_redline(args.docx_file, args.changes_file)
    else:
        mode_extract(args.docx_file)

if __name__ == "__main__":
    main()