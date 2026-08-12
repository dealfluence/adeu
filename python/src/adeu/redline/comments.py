import datetime
import re
from typing import Dict, Optional

import structlog
from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import Part, XmlPart
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, nsmap, qn
from docx.oxml.xmlchemy import serialize_for_reading

from adeu.utils.long_hex_number import (
    generate_long_hex_number,
    is_word_readable_long_hex_number,
    to_long_hex_number,
)

logger = structlog.get_logger(__name__)

# Register w15 namespace globally for python-docx
w15_ns = "http://schemas.microsoft.com/office/word/2012/wordml"
if "w15" not in nsmap:
    nsmap["w15"] = w15_ns

# Register w14 namespace for paraId
w14_ns = "http://schemas.microsoft.com/office/word/2010/wordml"
if "w14" not in nsmap:
    nsmap["w14"] = w14_ns

# Register w16cid namespace for durableId
w16cid_ns = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
if "w16cid" not in nsmap:
    nsmap["w16cid"] = w16cid_ns

# Register w16cex namespace for commentExtensible
w16cex_ns = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
if "w16cex" not in nsmap:
    nsmap["w16cex"] = w16cex_ns

# Register w16se namespace (often used in ignorable)
if "w16se" not in nsmap:
    nsmap["w16se"] = "http://schemas.microsoft.com/office/word/2015/wordml/symex"

CT_EXTENDED = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
CT_IDS = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml"
CT_EXTENSIBLE = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml"

# ---------------------------------------------------------------------------
# Repairing ST_LongHexNumbers Adeu did not mint
# ---------------------------------------------------------------------------
#
# The generators guarantee that every id Adeu MINTS is one Word will keep. They
# can say nothing about the ids Adeu READS. A document arriving with
# `w14:paraId="D2AEAE20"` â€” legal against the schema, discarded by Word on load
# â€” takes the reply threaded onto it down with it, and no amount of correct
# minting prevents that (2026-08-12 B6, western-district demo).
#
# Repairing means rewriting a value that other parts point AT, so the attribute
# groups below exist to keep a repair from breaking the references it was
# supposed to preserve.

#: One logical paragraph identity, spelled four ways across three parts. Word
#: consults all of them; repair them together or the comment drops out of the
#: modern-comments path exactly as if it had not been repaired at all.
PARA_ID_ATTRIBUTES = ("w14:paraId", "w15:paraId", "w15:paraIdParent", "w16cid:paraId")

#: The comment's durable identity: commentsIds mints it, commentsExtensible
#: points back at it. Out of range, the anchor collapses to a point (B3).
DURABLE_ID_ATTRIBUTES = ("w16cid:durableId", "w16cex:durableId")

#: ST_LongHexNumbers nothing else references, so they can be folded in place.
#: Folding (rather than re-minting) keeps equal rsids equal, which is the only
#: thing an rsid means, and keeps `w14:textId` on the element whose `w14:paraId`
#: it versions â€” [MS-DOCX] 2.6.2.6 requires the two to travel together.
STANDALONE_ID_ATTRIBUTES = (
    "w14:textId",
    "w:rsidR",
    "w:rsidRPr",
    "w:rsidRDefault",
    "w:rsidP",
    "w:rsidDel",
    "w:rsidTr",
)


class CommentThreadingError(Exception):
    """
    Raised when a reply cannot be threaded onto its parent comment.

    A `reply` that quietly becomes a new top-level thread is worse than a
    failed call: `apply_review_actions` reports success, the agent believes it
    answered the reviewer, and it keeps acting on a success it never got
    (BUG_comment_threading_anchoring_and_typography.md B1). So threading is
    resolved BEFORE any XML is written, and an unresolvable parent is loud.
    """


class CommentsManager:
    """
    Manages the 'word/comments.xml' part of the DOCX package.
    """

    def __init__(self, doc):
        logger.debug("Initializing CommentsManager")
        self.doc = doc
        self._comments_part = None
        self._extended_part = None
        self._ids_part = None
        self._extensible_part = None
        self._next_id = None

    @property
    def comments_part(self):
        if self._comments_part is None:
            self._comments_part = self._get_or_create_comments_part()
            self._ensure_namespaces()
        return self._comments_part

    @property
    def extended_part(self):
        if self._extended_part is None:
            self._extended_part = self._get_or_create_extended_part()
        return self._extended_part

    @property
    def ids_part(self):
        if self._ids_part is None:
            self._ids_part = self._get_or_create_ids_part()
        return self._ids_part

    @property
    def extensible_part(self):
        if self._extensible_part is None:
            self._extensible_part = self._get_or_create_extensible_part()
        return self._extensible_part

    @property
    def next_id(self):
        if self._next_id is None:
            self._next_id = self._get_next_comment_id()
        return self._next_id

    @next_id.setter
    def next_id(self, value):
        self._next_id = value

    def _ensure_xml_part(self, part: Part) -> XmlPart:
        """
        Ensures a generic Part is upgraded to an XmlPart so we can manipulate it.
        CRITICAL: Updates existing relationships to point to the new object to prevent
        duplicate entries in the saved file.
        """
        if isinstance(part, XmlPart):
            return part

        logger.debug("Upgrading generic Part to XmlPart", partname=part.partname)
        # Create new XmlPart
        xml_part = XmlPart(part.partname, part.content_type, parse_xml(part.blob), part.package)

        # 1. Swap in package (source of truth for serialization)
        if part in part.package.parts:
            idx = part.package.parts.index(part)
            part.package.parts[idx] = xml_part

        # 2. Swap in Relationships (The Fix for Duplicate Warnings)
        # Scan relationships on the main document part and update targets
        for rel in self.doc.part.rels.values():
            # Skip external links (like hyperlinks) to avoid ValueError on .target_part
            if rel.is_external:
                continue

            if rel.target_part == part:
                rel._target = xml_part

        return xml_part

    def _get_existing_part_by_type(self, content_type: str) -> Optional[Part]:
        """
        Searches the entire package for a part with the given content type.
        This is safer than relying on Relationship Types which vary by Word version.
        """
        for part in self.doc.part.package.parts:
            if part.content_type == content_type:
                logger.debug(
                    "Found existing part by content type",
                    content_type=content_type,
                    partname=part.partname,
                )
                return part
        logger.debug("No existing part found for content type", content_type=content_type)
        return None

    def _link_part(self, part: XmlPart, rel_type: str) -> XmlPart:
        """
        Ensures the main document part has a relationship to the given part.
        """
        # Check if already related (via python-docx internal cache)
        if part in self.doc.part.related_parts.values():
            return part

        # Check relationships manually to be safe (in case cache is stale)
        for rel in self.doc.part.rels.values():
            # Skip external relationships to prevent ValueError on target_part
            if rel.is_external:
                continue
            if rel.target_part == part:
                return part

        # Create relationship if missing
        logger.info(
            "Creating relationship to existing part",
            partname=part.partname,
            rel_type=rel_type,
        )
        self.doc.part.relate_to(part, rel_type)
        return part

    def _get_or_create_comments_part(self):
        content_type = CT.WML_COMMENTS

        # 1. Find existing by Content Type
        part = self._get_existing_part_by_type(content_type)

        if part:
            part = self._ensure_xml_part(part)
            return self._link_part(part, RT.COMMENTS)

        # 2. Create new part if not found
        package = self.doc.part.package
        partname = package.next_partname("/word/comments%d.xml")

        # Ensure root element declares namespaces and Ignorable
        # Word is strict: extended namespaces like w14/w15 must be flagged Ignorable
        # for backward compatibility, otherwise the attributes might be dropped.
        xml_bytes = (
            f"<w:comments {nsdecls('w', 'w14', 'w15')} "
            f'xmlns:w16cid="{w16cid_ns}" xmlns:w16cex="{w16cex_ns}" '
            f'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            f'mc:Ignorable="w14 w15 w16cid w16cex">\n'
            f"</w:comments>"
        ).encode("utf-8")

        logger.info("Creating new comments part", partname=partname)
        comments_part = XmlPart(partname, content_type, parse_xml(xml_bytes), package)
        package.parts.append(comments_part)
        self.doc.part.relate_to(comments_part, RT.COMMENTS)

        return comments_part

    def _get_or_create_extended_part(self) -> XmlPart:
        RELTYPE_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
        CONTENT_TYPE_EXTENDED = CT_EXTENDED

        part = self._get_existing_part_by_type(CONTENT_TYPE_EXTENDED)
        if part:
            part = self._ensure_xml_part(part)
            return self._link_part(part, RELTYPE_EXTENDED)

        package = self.doc.part.package
        partname = package.next_partname("/word/commentsExtended%d.xml")

        xml_bytes = (f"<w15:commentsEx xmlns:w15='{w15_ns}'></w15:commentsEx>").encode("utf-8")

        logger.info("Creating new extended part", partname=partname)
        extended_part = XmlPart(partname, CONTENT_TYPE_EXTENDED, parse_xml(xml_bytes), package)
        package.parts.append(extended_part)
        self.doc.part.relate_to(extended_part, RELTYPE_EXTENDED)

        return extended_part

    def _get_or_create_ids_part(self) -> XmlPart:
        RELTYPE_IDS = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
        CONTENT_TYPE_IDS = CT_IDS

        part = self._get_existing_part_by_type(CONTENT_TYPE_IDS)
        if part:
            part = self._ensure_xml_part(part)
            return self._link_part(part, RELTYPE_IDS)

        package = self.doc.part.package
        partname = package.next_partname("/word/commentsIds%d.xml")

        xml_bytes = (f"<w16cid:commentsIds {nsdecls('w16cid')}></w16cid:commentsIds>").encode("utf-8")

        logger.info("Creating new ids part", partname=partname)
        ids_part = XmlPart(partname, CONTENT_TYPE_IDS, parse_xml(xml_bytes), package)
        package.parts.append(ids_part)
        self.doc.part.relate_to(ids_part, RELTYPE_IDS)

        return ids_part

    def _get_or_create_extensible_part(self) -> XmlPart:
        RELTYPE_EXTENSIBLE = "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"
        CONTENT_TYPE_EXTENSIBLE = CT_EXTENSIBLE

        part = self._get_existing_part_by_type(CONTENT_TYPE_EXTENSIBLE)
        if part:
            part = self._ensure_xml_part(part)
            return self._link_part(part, RELTYPE_EXTENSIBLE)

        package = self.doc.part.package
        partname = package.next_partname("/word/commentsExtensible%d.xml")

        xml_bytes = (f"<w16cex:commentsExtensible {nsdecls('w16cex')}></w16cex:commentsExtensible>").encode("utf-8")

        logger.info("Creating new extensible part", partname=partname)
        extensible_part = XmlPart(partname, CONTENT_TYPE_EXTENSIBLE, parse_xml(xml_bytes), package)
        package.parts.append(extensible_part)
        self.doc.part.relate_to(extensible_part, RELTYPE_EXTENSIBLE)

        return extensible_part

    def _ensure_namespaces(self):
        if not self._comments_part:
            return

        element = self._comments_part.element
        has_w14 = "w14" in element.nsmap and element.nsmap["w14"] == w14_ns
        has_w15 = "w15" in element.nsmap and element.nsmap["w15"] == w15_ns

        # Check for mc:Ignorable
        # This is harder to check via nsmap, checking string serialization is robust
        xml_str = serialize_for_reading(element)
        has_ignorable = "mc:Ignorable" in xml_str and "w14" in xml_str and "w15" in xml_str

        if has_w14 and has_w15 and has_ignorable:
            return

        # Brute force update of the root tag

        # Check if the existing root tag is self-closing (e.g. <w:comments ... />)
        # This happens if the comments part is empty.
        match = re.search(r"<w:comments[^>]*>", xml_str)
        if not match:
            return

        original_tag = match.group(0)
        is_self_closing = original_tag.strip().endswith("/>")

        # We reconstruct the opening tag with all needed namespaces and Ignorable
        replacement = (
            f'<w:comments xmlns:w="{nsmap["w"]}" xmlns:w14="{w14_ns}" xmlns:w15="{w15_ns}" '
            f'xmlns:w16cid="{w16cid_ns}" xmlns:w16cex="{w16cex_ns}" '
            f'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            f'mc:Ignorable="w14 w15 w16cid w16cex">'
        )

        if is_self_closing:
            replacement += "</w:comments>"

        logger.debug(
            "Patching root element namespaces",
            original=xml_str[:100],
            is_self_closing=is_self_closing,
        )

        # Replace the matched tag with our new tag(s)
        new_xml = xml_str.replace(original_tag, replacement, 1)
        self._comments_part._element = parse_xml(new_xml)

    def _get_next_comment_id(self) -> int:
        ids = [0]
        part = self._get_existing_part_by_type(CT.WML_COMMENTS)
        if part:
            comments = parse_xml(part.blob).findall(qn("w:comment"))
            for c in comments:
                try:
                    ids.append(int(c.get(qn("w:id"))))
                except (ValueError, TypeError):
                    pass
        return max(ids) + 1

    # Every id below is an ST_LongHexNumber and comes from ONE generator.
    #
    # These stay as named aliases only so the call sites read as what they
    # mint; they must never diverge. Word parses ST_LongHexNumber as a SIGNED
    # 32-bit integer for ALL of them, silently discarding and regenerating
    # anything outside (0x00000000, 0x80000000) â€” an out-of-range paraId drops
    # replies out of their thread (B5), an out-of-range durableId collapses the
    # comment's anchor (B3), and a zero paraId makes Word reject the file
    # outright. The earlier belief that only durableId was constrained is what
    # produced B5; see adeu.utils.long_hex_number and
    # BUG_paraId_signed_int32_thread_collapse.md.

    def _generate_para_id(self) -> str:
        """`w14:paraId` â€” the identity `w15:paraIdParent` threads onto."""
        return generate_long_hex_number()

    def _generate_durable_id(self) -> str:
        """`w16cid:durableId` â€” the identity the comment anchor binds to."""
        return generate_long_hex_number()

    def _generate_rsid(self) -> str:
        """`w:rsidR` / `w:rsidRDefault` / `w:rsidP` â€” revision-save grouping."""
        return generate_long_hex_number()

    def _get_initials(self, author: str) -> str:
        if not author:
            return ""
        return "".join(part[0] for part in author.split() if part).upper()

    def _has_comments_part(self) -> bool:
        """
        True when the package already carries a comments part (loaded or not).

        Read paths must use this guard instead of testing the raw backing field
        `self._comments_part`: on a fresh manager the backing field is None
        until the lazy `comments_part` property populates it, so guarding on
        the field silently no-ops even though the document HAS comments, and
        sanitize then reports comments removed while word/comments.xml
        survives intact (QA 2026-07-17 F3). Checking the
        package (rather than unconditionally touching the property) keeps the
        other guarantee: a document with no comments part never has one
        created as a side effect of a read/delete.
        """
        return self._comments_part is not None or self._get_existing_part_by_type(CT.WML_COMMENTS) is not None

    def _find_para_id_for_comment(self, comment_id: str) -> Optional[str]:
        if not self._has_comments_part():
            return None
        for c in self.comments_part.element.findall(qn("w:comment")):
            if c.get(qn("w:id")) == comment_id:
                for p in c.findall(qn("w:p")):
                    pid = p.get(qn("w14:paraId"))
                    if pid:
                        return pid
        return None

    def _find_thread_root_para_id(self, comment_id: str) -> Optional[str]:
        """
        Finds the 'paraId' of the ROOT comment in the thread.
        Modern Word flattens all replies to point to the original comment.
        """
        direct_para_id = self._find_para_id_for_comment(comment_id)
        ext_part = self._get_existing_part_by_type(CT_EXTENDED)
        if not direct_para_id or not ext_part:
            return direct_para_id

        ext_xml = parse_xml(ext_part.blob)
        for child in ext_xml:
            if child.get(qn("w15:paraId")) == direct_para_id:
                parent = child.get(qn("w15:paraIdParent"))
                if parent:
                    return parent
        return direct_para_id

    def _existing_comment_part_elements(self) -> list:
        """Every comment part that ALREADY exists, as a mutable element.

        Deliberately not the `comments_part` / `extended_part` properties: those
        CREATE the part they cannot find, and a repair pass that invents a
        commentsExtended part for a document that has no comments would be a
        side effect nobody asked for.
        """
        elements = []
        for content_type in (CT.WML_COMMENTS, CT_EXTENDED, CT_IDS, CT_EXTENSIBLE):
            part = self._get_existing_part_by_type(content_type)
            if part is not None:
                elements.append(self._ensure_xml_part(part).element)
        return elements

    def _free_long_hex_number(self, value: str, taken: set) -> str:
        """A legal id for `value` that the comment parts are not already using.

        Folding first (clearing the top bit, which is what Word does to the
        value anyway) keeps the repair DETERMINISTIC: the same document repaired
        twice produces the same ids, so a re-run is a no-op rather than a fresh
        set of anchors. `D2AEAE20 -> 52AEAE20`, Word-verified against the
        western-district document.

        The collision check is not belt-and-braces. [MS-DOCX] 2.6.2.4 requires
        `w14:paraId` to be unique within the part, and folding is exactly the
        operation that can violate it: `D2AEAE20` and `52AEAE20` fold to the
        same value, so a document containing both would end up with one id
        naming two paragraphs and a `w15:paraIdParent` that no longer says
        which thread it means.
        """
        try:
            candidate = to_long_hex_number(int(value, 16))
        except ValueError:
            candidate = generate_long_hex_number()
        while candidate in taken:
            candidate = generate_long_hex_number()
        return candidate

    def _repair_inherited_long_hex_numbers(self) -> None:
        """Bring every ST_LongHexNumber in the comment parts into range.

        B5 masked the generators, which fixed every id Adeu mints and none of
        the ids it inherits. B6 is the second kind: the western-district demo
        was handed a comment carrying `w14:paraId="D2AEAE20"`,
        `_adopt_into_modern_comments` reused it verbatim because it was
        present, and the reply's `w15:paraIdParent` was written to point at a
        value Word discards on load. Every check passed on the way out â€” the
        reply IS parented, `CommentThreadingError` correctly did not fire â€” and
        the thread still collapsed the moment the document was opened.

        Whole-part, not just the comment being replied to: Word renumbers a
        PART when it finds a bad id in it, so leaving one bad rsid behind in
        comments.xml re-arms the renumbering pass that de-threads the reply.

        A no-op on a healthy document. It must stay that way: a pass that
        re-mints unconditionally would churn every paraId on every save and
        invalidate every `{#cell:<paraId>}` anchor the caller is holding, which
        is the damage it exists to prevent.
        """
        elements = self._existing_comment_part_elements()
        if not elements:
            return

        def remap_for(attributes) -> Dict[str, str]:
            taken, broken = set(), []
            for element in elements:
                for el in element.iter():
                    for attribute in attributes:
                        value = el.get(qn(attribute))
                        if not value:
                            continue
                        if is_word_readable_long_hex_number(value):
                            taken.add(value.upper())
                        elif value not in broken:
                            broken.append(value)
            remap: Dict[str, str] = {}
            for value in broken:
                repaired = self._free_long_hex_number(value, taken)
                taken.add(repaired)
                remap[value] = repaired
            return remap

        para_remap = remap_for(PARA_ID_ATTRIBUTES)
        durable_remap = remap_for(DURABLE_ID_ATTRIBUTES)

        standalone = 0
        for element in elements:
            for el in element.iter():
                for attribute in PARA_ID_ATTRIBUTES:
                    value = el.get(qn(attribute))
                    if value in para_remap:
                        el.set(qn(attribute), para_remap[value])
                for attribute in DURABLE_ID_ATTRIBUTES:
                    value = el.get(qn(attribute))
                    if value in durable_remap:
                        el.set(qn(attribute), durable_remap[value])
                for attribute in STANDALONE_ID_ATTRIBUTES:
                    value = el.get(qn(attribute))
                    if value and not is_word_readable_long_hex_number(value):
                        el.set(qn(attribute), self._free_long_hex_number(value, set()))
                        standalone += 1

        if para_remap or durable_remap or standalone:
            logger.info(
                "Repaired inherited ST_LongHexNumbers Word would have discarded",
                para_ids=para_remap,
                durable_ids=durable_remap,
                standalone=standalone,
            )

    def _adopt_into_modern_comments(self, comment_id: str) -> Optional[str]:
        """
        Gives an existing comment a modern paragraph identity so a reply can
        thread onto it, and returns that paraId (None if the comment does not
        exist at all).

        A comment written by pre-2013 Word â€” or by any generator that skips the
        modern-comments extensions â€” has no `w14:paraId`, so
        `_find_thread_root_para_id` resolves nothing and `w15:paraIdParent`
        never gets written: the "reply" silently becomes a second top-level
        thread (B1). Minting the missing identity is the repair; it is
        idempotent and leaves the comment's body, author and date untouched.

        The paraId is registered in commentsExtended AND commentsIds together:
        Word consults both, and a paraId present in one but not the other drops
        the comment out of the modern-comments path entirely.
        """
        if not self._has_comments_part():
            return None

        comment_el = None
        for c in self.comments_part.element.findall(qn("w:comment")):
            if c.get(qn("w:id")) == str(comment_id):
                comment_el = c
                break
        if comment_el is None:
            return None

        paragraphs = comment_el.findall(qn("w:p"))
        if not paragraphs:
            return None

        para_id = next((p.get(qn("w14:paraId")) for p in paragraphs if p.get(qn("w14:paraId"))), None)
        if not para_id:
            para_id = self._generate_para_id()
            paragraphs[0].set(qn("w14:paraId"), para_id)
            logger.info(
                "Minted a modern paraId for a legacy comment so a reply can thread onto it",
                comment_id=str(comment_id),
                para_id=para_id,
            )

        if self.extended_part is not None and not any(
            child.get(qn("w15:paraId")) == para_id for child in self.extended_part.element
        ):
            # Thread ROOT: no w15:paraIdParent.
            self._add_to_extended_part(para_id, None)

        if self.ids_part is not None and not any(
            child.get(qn("w16cid:paraId")) == para_id for child in self.ids_part.element
        ):
            self._add_to_ids_part(para_id)

        return para_id

    def resolve_thread_parent_para_id(self, parent_id: str) -> Optional[str]:
        """
        The paraId a reply to `parent_id` must point at, repairing a parent that
        predates modern comments. None means threading is impossible and the
        caller must fail loudly rather than mint a top-level comment.

        The repair pass runs FIRST, because every lookup below reads paraIds
        and a lookup that returns an id Word discards is worse than one that
        returns nothing: `None` raises CommentThreadingError and leaves the
        document alone, while a doomed id is reported as a successful reply and
        collapses the thread on load (B6).

        The root lookup runs next so a reply-to-a-reply still flattens onto the
        thread root (modern Word's model). The adoption pass then runs
        unconditionally â€” it is idempotent, and it also backfills a parent that
        HAS a w14:paraId but is missing from commentsExtended / commentsIds:
        Word consults both, so a paraIdParent pointing at an unregistered
        paragraph drops the reply out of its thread just as surely as a missing
        attribute would.
        """
        self._repair_inherited_long_hex_numbers()
        root_para_id = self._find_thread_root_para_id(str(parent_id))
        adopted_para_id = self._adopt_into_modern_comments(str(parent_id))
        return root_para_id or adopted_para_id

    def _add_to_extended_part(self, para_id: str, parent_para_id: Optional[str]):
        if not self.extended_part:
            return
        comment_ex = OxmlElement("w15:commentEx")
        comment_ex.set(qn("w15:paraId"), para_id)
        if parent_para_id:
            comment_ex.set(qn("w15:paraIdParent"), parent_para_id)
        comment_ex.set(qn("w15:done"), "0")
        self.extended_part.element.append(comment_ex)

    def _add_to_ids_part(self, para_id: str):
        if not self.ids_part:
            return
        comment_id_el = OxmlElement("w16cid:commentId")
        comment_id_el.set(qn("w16cid:paraId"), para_id)
        comment_id_el.set(qn("w16cid:durableId"), self._generate_durable_id())
        self.ids_part.element.append(comment_id_el)

    def _add_to_extensible_part(self, para_id: str, date_utc: str):
        if not self.extensible_part or not self.ids_part:
            return
        durable_id = None
        for child in self.ids_part.element:
            if child.get(qn("w16cid:paraId")) == para_id:
                durable_id = child.get(qn("w16cid:durableId"))
                break
        if durable_id:
            ext_el = OxmlElement("w16cex:commentExtensible")
            ext_el.set(qn("w16cex:durableId"), durable_id)
            ext_el.set(qn("w16cex:dateUtc"), date_utc)
            self.extensible_part.element.append(ext_el)

    def add_comment(self, author: str, text: str, parent_id: Optional[str] = None) -> str:
        logger.info("Adding comment", author=author, parent_id=parent_id)

        # Before anything else, and for top-level comments too: the paraIds this
        # document arrived with are about to share a part with the ones we are
        # about to mint, and Word renumbers the whole part if any of them is out
        # of range (B6).
        self._repair_inherited_long_hex_numbers()

        # Snapshot the modern-comments state BEFORE resolving threading: the
        # legacy `w15:p` fallback below keys on whether the document was
        # already on the modern path, and repairing a legacy parent may create
        # the commentsExtended part as a side effect.
        ext_part_existed = (
            self._get_existing_part_by_type(CT_EXTENDED)
            is not None
        )

        # Resolve threading BEFORE writing anything. A reply whose parent
        # cannot be resolved used to be written anyway, minus its
        # w15:paraIdParent â€” i.e. as a brand-new top-level thread, reported as
        # applied (B1). Failing here leaves the document untouched.
        parent_para_id: Optional[str] = None
        if parent_id is not None:
            parent_para_id = self.resolve_thread_parent_para_id(str(parent_id))
            if not parent_para_id:
                raise CommentThreadingError(
                    f"Cannot thread a reply onto comment Com:{parent_id}: the comment has no "
                    "resolvable paragraph identity (w14:paraId) in word/comments.xml, so Word "
                    "would render the reply as a separate top-level comment instead of a reply. "
                    "Refusing to create an unthreaded comment."
                )

        comment_id = str(self.next_id)
        self.next_id += 1
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

        comment = OxmlElement("w:comment")
        comment.set(qn("w:id"), comment_id)
        comment.set(qn("w:author"), author)
        comment.set(qn("w:date"), now)

        initials = self._get_initials(author)
        if initials:
            comment.set(qn("w:initials"), initials)

        # Legacy Threading (w15:p)
        # We only add this if we are NOT using modern comments (extended_part),
        # as modern Word relies on the extended part, and providing both might cause conflicts.
        # Only add if Modern Comments (extended) are NOT in use to avoid conflicts.
        if parent_id and not ext_part_existed:
            comment.set(qn("w15:p"), str(parent_id))

        para_id = self._generate_para_id()
        rsid = self._generate_rsid()

        p = OxmlElement("w:p")
        p.set(qn("w14:paraId"), para_id)
        p.set(qn("w14:textId"), "77777777")
        p.set(qn("w:rsidR"), rsid)
        p.set(qn("w:rsidRDefault"), rsid)
        p.set(qn("w:rsidP"), rsid)

        pPr = OxmlElement("w:pPr")
        pStyle = OxmlElement("w:pStyle")
        pStyle.set(qn("w:val"), "CommentText")
        pPr.append(pStyle)
        p.append(pPr)

        r_ref = OxmlElement("w:r")
        rPr_ref = OxmlElement("w:rPr")
        rStyle_ref = OxmlElement("w:rStyle")
        rStyle_ref.set(qn("w:val"), "CommentReference")
        rPr_ref.append(rStyle_ref)
        r_ref.append(rPr_ref)
        r_ref.append(OxmlElement("w:annotationRef"))
        p.append(r_ref)

        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = text

        r.append(t)
        p.append(r)
        comment.append(p)

        self.comments_part.element.append(comment)

        if self.extended_part:
            # parent_para_id was resolved (and any legacy parent repaired) at
            # the top of this method, so it is either a real thread root or the
            # call already raised. Never re-resolve here: silently falling back
            # to None is exactly how a reply became a thread root (B1).
            self._add_to_extended_part(para_id, parent_para_id)

        if self.ids_part:
            self._add_to_ids_part(para_id)

        if self.extensible_part:
            self._add_to_extensible_part(para_id, now)

        return comment_id

    def extract_comments_data(self) -> Dict[str, dict]:
        data: Dict[str, dict] = {}
        part = self._get_existing_part_by_type(CT.WML_COMMENTS)
        if not part:
            return data

        # Map paraId -> comment_id to resolve parents from commentsExtended
        para_id_to_cid: Dict[str, str] = {}

        comments = parse_xml(part.blob).findall(qn("w:comment"))
        for c in comments:
            c_id = c.get(qn("w:id"))
            c_author = c.get(qn("w:author")) or "Unknown"
            c_date = c.get(qn("w:date")) or ""

            is_resolved = False
            val = c.get(qn("w15:done"))
            if val in ("1", "true", "on"):
                is_resolved = True

            parent_id = c.get("{http://schemas.microsoft.com/office/word/2012/wordml}p")
            if not parent_id:
                # Fallback: check for prefixed attribute if namespace wasn't resolved correctly
                parent_id = c.get("w15:p")

            # Capture paraId for extended threading lookup
            # Usually in the first paragraph of the comment
            for p_elem in c.findall(qn("w:p")):
                pid = p_elem.get(qn("w14:paraId"))
                if pid:
                    para_id_to_cid[pid] = c_id

            text_parts = []
            for p in c.findall(qn("w:p")):
                for r in p.findall(qn("w:r")):
                    for t in r.findall(qn("w:t")):
                        if t.text:
                            text_parts.append(t.text)
                text_parts.append("\n")

            full_text = "".join(text_parts).strip()

            data[c_id] = {
                "author": c_author,
                "text": full_text,
                "date": c_date,
                "resolved": is_resolved,
                "parent_id": parent_id,
            }

        # 2. Enrich with Threading and Resolved status from commentsExtended (Modern Word)
        ext_part = self._get_existing_part_by_type(CT_EXTENDED)
        if ext_part:
            try:
                ext_xml = parse_xml(ext_part.blob)
                for child in ext_xml:
                    para_id = child.get(qn("w15:paraId"))
                    parent_para_id = child.get(qn("w15:paraIdParent"))
                    done_val = child.get(qn("w15:done"))

                    if para_id:
                        c_id = para_id_to_cid.get(para_id)
                        if c_id and c_id in data:
                            if parent_para_id:
                                p_id = para_id_to_cid.get(parent_para_id)
                                if p_id:
                                    data[c_id]["parent_id"] = p_id
                            if done_val in ("1", "true", "on"):
                                data[c_id]["resolved"] = True
            except Exception as e:
                logger.warning("Failed to parse commentsExtended for threading/resolved status", error=str(e))

        return data

    def delete_comment(self, comment_id: str):
        """
        Safely deletes a comment and all its metadata from the 4 XML parts.
        Also recursively deletes any threaded replies attached to this comment.
        """
        if not self._has_comments_part():
            return
        comments_part = self.comments_part

        comment_id_str = str(comment_id)
        comment_el = None

        # 1. Find the comment element
        for c in comments_part.element.findall(qn("w:comment")):
            if c.get(qn("w:id")) == comment_id_str:
                comment_el = c
                break

        if comment_el is None:
            return

        # 2. Extract paraId (required to find it in the auxiliary parts)
        para_id = None
        for p in comment_el.findall(qn("w:p")):
            pid = p.get(qn("w14:paraId"))
            if pid:
                para_id = pid
                break

        if para_id:
            # 3. Handle threaded replies: if we delete the parent, delete the replies
            replies_to_delete = []
            if self.extended_part:
                for child in self.extended_part.element:
                    if child.get(qn("w15:paraIdParent")) == para_id:
                        child_para_id = child.get(qn("w15:paraId"))
                        if child_para_id:
                            # Map child paraId back to comment ID
                            for c in comments_part.element.findall(qn("w:comment")):
                                for p in c.findall(qn("w:p")):
                                    if p.get(qn("w14:paraId")) == child_para_id:
                                        replies_to_delete.append(c.get(qn("w:id")))
                                        break

            for rep_id in replies_to_delete:
                if rep_id:
                    self.delete_comment(rep_id)

            # 4. Clean up auxiliary parts for THIS comment
            durable_id = None

            # a. commentsIds.xml
            if self.ids_part:
                for child in list(self.ids_part.element):
                    if child.get(qn("w16cid:paraId")) == para_id:
                        durable_id = child.get(qn("w16cid:durableId"))
                        self.ids_part.element.remove(child)

            # b. commentsExtended.xml
            if self.extended_part:
                for child in list(self.extended_part.element):
                    if child.get(qn("w15:paraId")) == para_id:
                        self.extended_part.element.remove(child)

            # c. commentsExtensible.xml
            if durable_id and self.extensible_part:
                for child in list(self.extensible_part.element):
                    if child.get(qn("w16cex:durableId")) == durable_id:
                        self.extensible_part.element.remove(child)

        # 5. Finally, remove from comments.xml
        if comment_el.getparent() is not None:
            comment_el.getparent().remove(comment_el)
