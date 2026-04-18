import argparse
import sys
import zipfile
from pathlib import Path

from lxml import etree

NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "dc": "http://purl.org/dc/elements/1.1/",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dcterms": "http://purl.org/dc/terms/",
    "app": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
}


def parse_xml_part(zf: zipfile.ZipFile, filepath: str) -> etree._ElementTree | None:
    try:
        with zf.open(filepath) as f:
            return etree.parse(f)
    except KeyError:
        return None


def assert_empty(elements, message):
    if elements:
        lines = [f"❌ FAIL: {message}"]
        for el in elements[:3]:
            lines.append(f"   Found: {etree.tostring(el).decode('utf-8').strip()}")
        if len(elements) > 3:
            lines.append(f"   ... and {len(elements) - 3} more.")
        raise AssertionError("\n".join(lines))


def check_global_scrub(zf: zipfile.ZipFile):
    """Checks that apply to ALL sanitized documents, regardless of mode."""
    # 1. Check Document XML
    doc_tree = parse_xml_part(zf, "word/document.xml")
    if doc_tree:
        # RSIDs
        rsids = doc_tree.xpath(
            "//*[@w:rsidR or @w:rsidRPr or @w:rsidRDefault or @w:rsidP or @w:rsidDel or @w:rsidSect or @w:rsidTr]",
            namespaces=NAMESPACES,
        )
        assert_empty(rsids, "RSID attributes found in document.xml")

        # Paragraph/Text IDs
        para_ids = doc_tree.xpath("//*[@w14:paraId or @w14:textId]", namespaces=NAMESPACES)
        assert_empty(para_ids, "w14:paraId or w14:textId found in document.xml")

        # Proofing Errors
        proof_errs = doc_tree.xpath("//w:proofErr", namespaces=NAMESPACES)
        assert_empty(proof_errs, "w:proofErr markers found in document.xml")

        # Hidden text
        hidden = doc_tree.xpath("//w:rPr/w:vanish | //w:rPr/w:webHidden", namespaces=NAMESPACES)
        assert_empty(hidden, "Hidden text properties found in document.xml")

    # 2. Check Core Properties (docProps/core.xml)
    core_tree = parse_xml_part(zf, "docProps/core.xml")
    if core_tree:
        creators = core_tree.xpath("//dc:creator/text()", namespaces=NAMESPACES)
        if creators and creators[0].strip():
            raise AssertionError(f"❌ FAIL: dc:creator not scrubbed. Found: {creators[0]}")

        modifiers = core_tree.xpath("//cp:lastModifiedBy/text()", namespaces=NAMESPACES)
        if modifiers and modifiers[0].strip():
            raise AssertionError(f"❌ FAIL: cp:lastModifiedBy not scrubbed. Found: {modifiers[0]}")

        # Timestamps should be epoch
        for tag in ["dcterms:created", "dcterms:modified"]:
            times = core_tree.xpath(f"//{tag}/text()", namespaces=NAMESPACES)
            if times and times[0] != "1970-01-01T00:00:00Z":
                raise AssertionError(f"❌ FAIL: {tag} not normalized. Found: {times[0]}")

    # 3. Check App Properties (docProps/app.xml)
    app_tree = parse_xml_part(zf, "docProps/app.xml")
    if app_tree:
        for tag in ["app:TotalTime", "app:Template", "app:Company", "app:Manager"]:
            vals = app_tree.xpath(f"//{tag}/text()", namespaces=NAMESPACES)
            if vals and vals[0].strip() and vals[0].strip() != "0":
                raise AssertionError(f"❌ FAIL: {tag} not scrubbed. Found: {vals[0]}")

    # 4. Check for Custom XML parts
    custom_xml_files = [f for f in zf.namelist() if f.startswith("customXml/")]
    if custom_xml_files:
        raise AssertionError(f"❌ FAIL: Custom XML parts were not stripped: {custom_xml_files}")


def check_full_scrub(zf: zipfile.ZipFile):
    """Checks specific to --accept-all (Full) mode."""
    doc_tree = parse_xml_part(zf, "word/document.xml")
    if doc_tree:
        changes = doc_tree.xpath("//w:ins | //w:del | //w:rPrChange | //w:pPrChange", namespaces=NAMESPACES)
        assert_empty(changes, "Track changes found in Full Sanitize mode")

        comments = doc_tree.xpath(
            "//w:commentRangeStart | //w:commentRangeEnd | //w:commentReference", namespaces=NAMESPACES
        )
        assert_empty(comments, "Comment markers found in document.xml in Full Sanitize mode")

    if "word/comments.xml" in zf.namelist():
        raise AssertionError("❌ FAIL: word/comments.xml still exists in package.")


def check_keep_markup(zf: zipfile.ZipFile, expected_author: str):
    """Checks specific to --keep-markup mode."""
    doc_tree = parse_xml_part(zf, "word/document.xml")
    if doc_tree:
        # Check that any existing changes have the correct author
        changes = doc_tree.xpath("//w:ins | //w:del | //w:rPrChange | //w:pPrChange", namespaces=NAMESPACES)
        for c in changes:
            author = c.get(f"{{{NAMESPACES['w']}}}author")
            if author and author != expected_author:
                raise AssertionError(
                    f"❌ FAIL: Track change author '{author}' does not match expected '{expected_author}'"
                )

            date = c.get(f"{{{NAMESPACES['w']}}}date")
            if date and date != "2025-01-01T00:00:00Z":
                raise AssertionError(f"❌ FAIL: Track change date '{date}' was not normalized")

    comments_tree = parse_xml_part(zf, "word/comments.xml")
    if comments_tree:
        comments = comments_tree.xpath("//w:comment", namespaces=NAMESPACES)
        for c in comments:
            author = c.get(f"{{{NAMESPACES['w']}}}author")
            if author and author != expected_author:
                raise AssertionError(f"❌ FAIL: Comment author '{author}' does not match expected '{expected_author}'")

    # Check for resolved comments in commentsExtended.xml
    ext_tree = parse_xml_part(zf, "word/commentsExtended.xml")
    if ext_tree:
        resolved = ext_tree.xpath("//w15:commentEx[@w15:done='1']", namespaces=NAMESPACES)
        assert_empty(resolved, "Resolved comments found in commentsExtended.xml (they should be stripped)")


def main():
    parser = argparse.ArgumentParser(description="Strictly validates a sanitized DOCX file.")
    parser.add_argument("file", type=Path, help="Path to the DOCX file")
    parser.add_argument("--mode", choices=["full", "keep"], required=True, help="Sanitization mode used")
    parser.add_argument("--author", type=str, default="My Firm", help="Expected author for keep mode")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"File not found: {args.file}")
        sys.exit(1)

    print(f"🔍 Validating {args.file.name} (Mode: {args.mode})...")

    try:
        with zipfile.ZipFile(args.file, "r") as zf:
            check_global_scrub(zf)

            if args.mode == "full":
                check_full_scrub(zf)
            elif args.mode == "keep":
                check_keep_markup(zf, args.author)

        print(f"✅ PASS: {args.file.name} is mathematically clean.")
    except AssertionError as e:
        print(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
