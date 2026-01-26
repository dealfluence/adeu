import datetime
import random
from typing import Dict, Optional

import structlog
from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import Part, XmlPart
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, nsmap, qn
from docx.oxml.xmlchemy import serialize_for_reading

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


class CommentsManager:
    """
    Manages the 'word/comments.xml' part of the DOCX package.
    """

    def __init__(self, doc):
        logger.debug("Initializing CommentsManager")
        self.doc = doc
        self.comments_part = self._get_or_create_comments_part()
        self._ensure_namespaces()
        self.extended_part = self._get_or_create_extended_part()
        self.ids_part = self._get_or_create_ids_part()
        self.extensible_part = self._get_or_create_extensible_part()
        self.next_id = self._get_next_comment_id()

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
        CONTENT_TYPE_EXTENDED = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"

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
        CONTENT_TYPE_IDS = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml"

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
        CONTENT_TYPE_EXTENSIBLE = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml"
        )

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
        if not self.comments_part:
            return

        element = self.comments_part.element
        has_w14 = "w14" in element.nsmap and element.nsmap["w14"] == w14_ns
        has_w15 = "w15" in element.nsmap and element.nsmap["w15"] == w15_ns

        # Check for mc:Ignorable
        # This is harder to check via nsmap, checking string serialization is robust
        xml_str = serialize_for_reading(element)
        has_ignorable = "mc:Ignorable" in xml_str and "w14" in xml_str and "w15" in xml_str

        if has_w14 and has_w15 and has_ignorable:
            return

        # Brute force update of the root tag
        # We reconstruct the opening tag with all needed namespaces and Ignorable
        replacement = (
            f'<w:comments xmlns:w="{nsmap["w"]}" xmlns:w14="{w14_ns}" xmlns:w15="{w15_ns}" '
            f'xmlns:w16cid="{w16cid_ns}" xmlns:w16cex="{w16cex_ns}" '
            f'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            f'mc:Ignorable="w14 w15 w16cid w16cex">'
        )
        logger.debug("Patching root element namespaces", original=xml_str[:100])
        # Regex replace or simple string replace of the first tag
        import re

        new_xml = re.sub(r"<w:comments[^>]*>", replacement, xml_str, count=1)
        self.comments_part._element = parse_xml(new_xml)

    def _get_next_comment_id(self) -> int:
        ids = [0]
        if self.comments_part:
            comments = self.comments_part.element.findall(qn("w:comment"))
            for c in comments:
                try:
                    ids.append(int(c.get(qn("w:id"))))
                except (ValueError, TypeError):
                    pass
        return max(ids) + 1

    def _generate_para_id(self) -> str:
        return f"{random.randint(0, 0xFFFFFFFF):08X}"

    def _generate_durable_id(self) -> str:
        return f"{random.randint(0, 0xFFFFFFFF):08X}"

    def _generate_rsid(self) -> str:
        return f"{random.randint(0, 0xFFFFFFFF):08X}"

    def _get_initials(self, author: str) -> str:
        if not author:
            return ""
        return "".join(part[0] for part in author.split() if part).upper()

    def _find_para_id_for_comment(self, comment_id: str) -> Optional[str]:
        if not self.comments_part:
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
        if not direct_para_id or not self.extended_part:
            return direct_para_id

        for child in self.extended_part.element:
            if child.get(qn("w15:paraId")) == direct_para_id:
                parent = child.get(qn("w15:paraIdParent"))
                if parent:
                    return parent
        return direct_para_id

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
        if parent_id and not self.extended_part:
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
            parent_para_id = None
            if parent_id:
                parent_para_id = self._find_thread_root_para_id(parent_id)
            self._add_to_extended_part(para_id, parent_para_id)

        if self.ids_part:
            self._add_to_ids_part(para_id)

        if self.extensible_part:
            self._add_to_extensible_part(para_id, now)

        return comment_id

    def extract_comments_data(self) -> Dict[str, dict]:
        data: Dict[str, dict] = {}
        if not self.comments_part:
            return data

        # Map paraId -> comment_id to resolve parents from commentsExtended
        para_id_to_cid: Dict[str, str] = {}

        comments = self.comments_part.element.findall(qn("w:comment"))
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

        # 2. Enrich with Threading from commentsExtended (Modern Word)
        if self.extended_part:
            try:
                # Iterate w15:commentEx elements
                # They look like: <w15:commentEx w15:paraId="..." w15:paraIdParent="..."/>
                for child in self.extended_part.element:
                    para_id = child.get(qn("w15:paraId"))
                    parent_para_id = child.get(qn("w15:paraIdParent"))

                    if para_id and parent_para_id:
                        c_id = para_id_to_cid.get(para_id)
                        p_id = para_id_to_cid.get(parent_para_id)
                        if c_id and p_id and c_id in data:
                            data[c_id]["parent_id"] = p_id
            except Exception as e:
                logger.warning("Failed to parse commentsExtended for threading", error=str(e))

        return data
