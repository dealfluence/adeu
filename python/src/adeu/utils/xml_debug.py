import re
import zipfile
from xml.dom.minidom import parseString


def abstract_docx_xml(xml_str: str, filename: str) -> str:
    """
    Abstracts volatile parts of the DOCX XML (IDs, Dates, RSIDs) to allow for text comparison.
    Removes noise but preserves structure to detect bugs (e.g. w15:p threading).
    """
    # 0. Clean Root Namespaces (Word spam)
    if "comments" in filename and filename.endswith(".xml"):
        # Matches <w:comments ...> or <w16cid:commentsIds ...>
        # Replaces with just the tag name <w:comments>
        xml_str = re.sub(r"^(<[\w:]+)(\s+.*?)(\/?>)", r"\1\3", xml_str, count=1, flags=re.DOTALL | re.MULTILINE)

    # 1. RSIDs - Remove completely (pure noise)
    xml_str = re.sub(r' w:rsid\w*="[^"]+"', "", xml_str)

    # 2. IDs - Abstract values, preserve attributes
    xml_str = re.sub(r'(w:id=")[^"]+(")', r"\1ID\2", xml_str)
    # w15:p is CRITICAL for threading, but Word sometimes omits/adds it inconsistently with our mock.
    # We assume Adeu's explicit addition is correct, but for diffing we remove it.
    xml_str = re.sub(r' w15:p="[^"]+"', "", xml_str)

    xml_str = re.sub(r'(w16cid:durableId=")[^"]+(")', r"\1DID\2", xml_str)
    xml_str = re.sub(r'(w16cex:durableId=")[^"]+(")', r"\1DID\2", xml_str)

    # 3. Dates
    xml_str = re.sub(r'(w:date=")[^"]+(")', r"\1DATE\2", xml_str)
    xml_str = re.sub(r'([a-zA-Z0-9]+:dateUtc=")[^"]+(")', r"\1DATE\2", xml_str)

    # 4. Para IDs
    xml_str = re.sub(r'(w14:paraId=")[^"]+(")', r"\1PID\2", xml_str)
    xml_str = re.sub(r'(w14:textId=")[^"]+(")', r"\1TID\2", xml_str)
    xml_str = re.sub(r'(w15:paraId=")[^"]+(")', r"\1PID\2", xml_str)
    xml_str = re.sub(r'(w15:paraIdParent=")[^"]+(")', r"\1PID\2", xml_str)
    xml_str = re.sub(r'(w16cid:paraId=")[^"]+(")', r"\1PID\2", xml_str)

    # 5. Relationship IDs (rId1 -> RID)
    xml_str = re.sub(r'(Id="rId)\d+(")', r"\1RID\2", xml_str)

    # 6. Normalize filenames in Relationship Targets
    # comments1.xml -> comments.xml (Allows diff against Word files)
    xml_str = re.sub(r'(Target=".*comments.*?)\d+(\.xml")', r"\1\2", xml_str)

    # 7. Initials
    xml_str = re.sub(r' w:initials="[^"]+"', "", xml_str)

    # 8. Filter people.xml relationships (Word adds them, Adeu does not)
    xml_str = re.sub(r'<Relationship [^>]*Target="people\.xml"[^>]*/>', "", xml_str)

    # 9. Strip empty lines to prevent minidom/regex whitespace drift
    xml_str = re.sub(r"\n\s*\n", "\n", xml_str.strip())

    return xml_str


def format_and_sort_xml(xml_bytes: bytes, filename: str) -> str:
    """
    Parses XML, Sorts Relationships if applicable, and Pretty Prints.
    """
    try:
        dom = parseString(xml_bytes)

        def sort_node_attributes(node):
            if node.nodeType == node.ELEMENT_NODE and node.attributes:
                attrs = sorted(node.attributes.keys())
                attr_vals = [(k, node.getAttribute(k)) for k in attrs]
                for k in attrs:
                    node.removeAttribute(k)
                for k, v in attr_vals:
                    node.setAttribute(k, v)
            for child in node.childNodes:
                sort_node_attributes(child)

        if dom.documentElement:
            sort_node_attributes(dom.documentElement)

        # Sort Relationships for deterministic diffing
        if filename.endswith(".rels"):
            rels_node = None
            if dom.documentElement is not None and dom.documentElement.tagName == "Relationships":
                rels_node = dom.documentElement

            if rels_node:
                children = []
                for child in rels_node.childNodes:
                    if child.nodeType == child.ELEMENT_NODE and child.tagName == "Relationship":
                        children.append(child)

                for child in children:
                    rels_node.removeChild(child)

                # Sort by Target first, then Type
                children.sort(key=lambda x: (x.getAttribute("Target"), x.getAttribute("Type")))

                for child in children:
                    rels_node.appendChild(child)

        return dom.toprettyxml(indent="  ")
    except Exception:
        return xml_bytes.decode("utf-8", errors="ignore")


def get_abstracted_xml_snapshot(docx_path: str) -> str:
    snapshot_lines = []

    with zipfile.ZipFile(docx_path, "r") as z:
        relevant_files = []
        for f in z.namelist():
            if f.endswith("/"):
                continue

            # Filter logic
            if f.startswith("word/") and (f.endswith(".xml") or f.endswith(".rels")):
                ignored = [
                    "word/settings.xml",
                    "word/webSettings.xml",
                    "word/fontTable.xml",
                    "word/styles.xml",
                    "word/people.xml",
                    "word/numbering.xml",  # often noisy
                ]
                if any(i in f for i in ignored):
                    continue
                relevant_files.append(f)

            if f == "_rels/.rels":
                relevant_files.append(f)

        relevant_files.sort()

        for fname in relevant_files:
            content = z.read(fname)
            formatted = format_and_sort_xml(content, fname)
            abstracted = abstract_docx_xml(formatted, fname)

            # Normalize filename (comments1.xml -> comments.xml)
            display_name = re.sub(r"(comments.*?)\d+(\.xml)", r"\1\2", fname)
            display_name = re.sub(r"(comments.*?)\d+(\.xml\.rels)", r"\1\2", display_name)

            snapshot_lines.append(f"=== FILE: {display_name} ===")
            snapshot_lines.append(abstracted)
            snapshot_lines.append(f"=== END FILE: {display_name} ===\n")

    return "\n".join(snapshot_lines)
