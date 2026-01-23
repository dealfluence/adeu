import argparse
import zipfile
import sys
import difflib
import os
from xml.dom.minidom import parseString, Node

# Extensions to treat as XML/Text for dumping purposes
TEXT_EXTENSIONS = {".xml", ".rels"}
# Explicitly skip binary content dumping for these
BINARY_EXTENSIONS = {".png", ".jpeg", ".jpg", ".emf", ".wmf", ".bin", ".wdp"}


def pretty_print_xml(xml_bytes, filename):
    """
    Parses XML, optionally sorts specific lists (Relationships, Content Types) 
    for stability, and returns a pretty-printed string.
    """
    try:
        # If it's not valid XML, minidom will raise. We fallback to utf-8 decode.
        dom = parseString(xml_bytes)
        root = dom.documentElement

        # 1. Sort Relationships in .rels files
        if filename.endswith(".rels") and root.tagName == "Relationships":
            sort_xml_children(root, "Relationship", lambda x: (x.getAttribute("Target"), x.getAttribute("Id")))

        # 2. Sort Content Types in [Content_Types].xml
        if filename.endswith("[Content_Types].xml") and root.tagName == "Types":
            # Sort <Default> by Extension, <Override> by PartName
            # We sort all children to ensure deterministic order regardless of interleaving
            def type_sort_key(node):
                tag = node.tagName
                if tag == "Default":
                    return (0, node.getAttribute("Extension"))
                elif tag == "Override":
                    return (1, node.getAttribute("PartName"))
                return (2, tag) # Fallback

            sort_xml_children(root, None, type_sort_key)

        # "toprettyxml" adds distinct newlines and indentation
        return dom.toprettyxml(indent="  ")
    except Exception:
        # Fallback for non-XML text files or malformed XML
        return xml_bytes.decode("utf-8", errors="ignore")


def sort_xml_children(parent, tag_name_filter, key_func):
    """
    Helper to sort XML children nodes in place.
    """
    children = []
    # Extract relevant element nodes
    for child in list(parent.childNodes):
        if child.nodeType == Node.ELEMENT_NODE:
            if tag_name_filter is None or child.tagName == tag_name_filter:
                children.append(child)
                parent.removeChild(child)
    
    # Sort
    children.sort(key=key_func)
    
    # Re-append
    for child in children:
        parent.appendChild(child)


def generate_docx_dump(path: str, concise: bool = False) -> str:
    """
    Generates a full textual snapshot of the DOCX contents.
    
    Args:
        path: Path to DOCX file.
        concise: If True (for diffing), omits volatile data like file sizes.
    """
    output = []
    
    output.append(f"=== Inspecting: {path} ===")
    
    if not os.path.exists(path):
        return f"Error: File not found: {path}"

    try:
        with zipfile.ZipFile(path, "r") as z:
            # 1. Sort files alphabetically for deterministic output
            all_files = sorted(z.infolist(), key=lambda x: x.filename)

            # 2. Summary Table
            output.append(f"\n[File List] Total Files: {len(all_files)}")
            
            if concise:
                # Minimalist list for diffing
                output.append("Filename")
                output.append("-" * 60)
                for info in all_files:
                    output.append(info.filename)
            else:
                # Detailed table for inspection
                output.append(f"{'Filename':<60} | {'Size':>10} | {'Compressed':>10}")
                output.append("-" * 86)
                for info in all_files:
                    output.append(f"{info.filename:<60} | {info.file_size:>10} | {info.compress_size:>10}")
            
            output.append("")

            # 3. File Contents
            for info in all_files:
                fname = info.filename
                ext = os.path.splitext(fname)[1].lower()
                
                # Skip folders
                if fname.endswith("/"):
                    continue

                output.append(f"\n=== FILE: {fname} ===")
                
                if ext in TEXT_EXTENSIONS or fname.endswith(".xml"):
                    content = z.read(fname)
                    formatted_xml = pretty_print_xml(content, fname)
                    output.append(formatted_xml.strip())
                elif ext in BINARY_EXTENSIONS:
                    output.append(f"[Binary Content: {ext}]")
                else:
                    # Try to decode as text, fallback to binary msg
                    try:
                        content = z.read(fname).decode('utf-8')
                        output.append(content)
                    except UnicodeDecodeError:
                        output.append(f"[Binary Content or Unknown Encoding]")
                        
                output.append(f"=== END FILE: {fname} ===")

    except Exception as e:
        output.append(f"Error inspecting file: {e}")
        import traceback
        output.append(traceback.format_exc())

    return "\n".join(output)


def cmd_inspect(args):
    # Full detail for inspection
    dump = generate_docx_dump(args.file, concise=False)
    print(dump)


def cmd_diff(args):
    print(f"Diffing {args.file_a} vs {args.file_b}...")
    
    # Concise mode for diffing (hides sizes)
    dump_a = generate_docx_dump(args.file_a, concise=True)
    dump_b = generate_docx_dump(args.file_b, concise=True)
    
    # Calculate unified diff
    diff_lines = difflib.unified_diff(
        dump_a.splitlines(),
        dump_b.splitlines(),
        fromfile=args.file_a,
        tofile=args.file_b,
        lineterm=""
    )
    
    diff_text = "\n".join(diff_lines)
    
    if not diff_text:
        print("Files are structurally identical.")
    else:
        print(diff_text)


def main():
    parser = argparse.ArgumentParser(description="Deep inspection and diffing of DOCX structure.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: inspect
    parser_inspect = subparsers.add_parser("inspect", help="Dump the structure and content of a single DOCX.")
    parser_inspect.add_argument("file", help="Path to DOCX file")
    parser_inspect.set_defaults(func=cmd_inspect)

    # Subcommand: diff
    parser_diff = subparsers.add_parser("diff", help="Compare structural differences between two DOCX files.")
    parser_diff.add_argument("file_a", help="Path to first DOCX file")
    parser_diff.add_argument("file_b", help="Path to second DOCX file")
    parser_diff.set_defaults(func=cmd_diff)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
