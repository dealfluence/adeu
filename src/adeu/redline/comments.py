import datetime
import random
from typing import Dict, Optional

from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import XmlPart
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, nsmap, qn
from docx.oxml.xmlchemy import serialize_for_reading

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

class CommentsManager:
    """
    Manages the 'word/comments.xml' part of the DOCX package.
    """

    def __init__(self, doc):
        self.doc = doc
        self.comments_part = self._get_or_create_comments_part()
        self._ensure_namespaces()
        self.extended_part = self._get_or_create_extended_part()
        self.ids_part = self._get_or_create_ids_part()
        self.extensible_part = self._get_or_create_extensible_part()
        self.next_id = self._get_next_comment_id()

    def _get_or_create_comments_part(self):
        """
        Retrieves the existing comments part or creates a new one
        linked to the main document part.
        """
        # 1. Check if comments part exists via relationships
        try:
            for rel in self.doc.part.rels.values():
                if rel.reltype == RT.COMMENTS:
                    return rel.target_part
        except Exception:
            pass

        # 2. Create new part if not found
        package = self.doc.part.package
        partname = package.next_partname("/word/comments%d.xml")
        content_type = CT.WML_COMMENTS

        # Ensure root element declares w15 namespace
        # We inject w14 and w15 immediately for new files
        xml_bytes = (f"<w:comments {nsdecls('w', 'w14', 'w15')}>\n</w:comments>").encode("utf-8")

        comments_part = XmlPart(partname, content_type, parse_xml(xml_bytes), package)
        package.parts.append(comments_part)
        self.doc.part.relate_to(comments_part, RT.COMMENTS)

        return comments_part

    def _get_or_create_extended_part(self) -> XmlPart:
        """
        Retrieves or creates the commentsExtended part.
        Required for Modern Comments threading.
        """
        RELTYPE_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
        CONTENT_TYPE_EXTENDED = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
        
        # 1. Check existing
        try:
            for rel in self.doc.part.rels.values():
                if rel.reltype == RELTYPE_EXTENDED:
                    part = rel.target_part
                    if not isinstance(part, XmlPart):
                        # If python-docx doesn't recognize the content type, it loads as generic Part.
                        # We must upgrade it to XmlPart to edit the XML.
                        
                        # Create new XmlPart
                        xml_part = XmlPart(
                            part.partname, 
                            part.content_type, 
                            parse_xml(part.blob), 
                            part.package
                        )
                        
                        # Swap in package (source of truth for serialization)
                        if part in part.package.parts:
                            idx = part.package.parts.index(part)
                            part.package.parts[idx] = xml_part
                            
                        # Swap in relationship (so we return the correct object)
                        rel._target = xml_part
                        return xml_part
                        
                    return part
        except Exception:
            pass
             
        # 2. Create new if missing
        package = self.doc.part.package
        partname = package.next_partname("/word/commentsExtended%d.xml")
        
        # Root element <w15:commentsEx>
        xml_bytes = (f"<w15:commentsEx {nsdecls('w15')}></w15:commentsEx>").encode("utf-8")
        
        extended_part = XmlPart(partname, CONTENT_TYPE_EXTENDED, parse_xml(xml_bytes), package)
        package.parts.append(extended_part)
        self.doc.part.relate_to(extended_part, RELTYPE_EXTENDED)
        
        return extended_part

    def _get_or_create_ids_part(self) -> XmlPart:
        """
        Retrieves or creates the commentsIds part.
        Required for Modern Comments durable IDs.
        """
        RELTYPE_IDS = "http://schemas.microsoft.com/office/2016/relationships/commentsIds"
        CONTENT_TYPE_IDS = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml"
        
        # 1. Check existing
        try:
            for rel in self.doc.part.rels.values():
                if rel.reltype == RELTYPE_IDS:
                    part = rel.target_part
                    if not isinstance(part, XmlPart):
                        xml_part = XmlPart(
                            part.partname, 
                            part.content_type, 
                            parse_xml(part.blob), 
                            part.package
                        )
                        if part in part.package.parts:
                            idx = part.package.parts.index(part)
                            part.package.parts[idx] = xml_part
                        rel._target = xml_part
                        return xml_part
                    return part
        except Exception:
            pass
             
        # 2. Create new if missing
        package = self.doc.part.package
        partname = package.next_partname("/word/commentsIds%d.xml")
        
        # Root element <w16cid:commentsIds>
        xml_bytes = (f"<w16cid:commentsIds {nsdecls('w16cid')}></w16cid:commentsIds>").encode("utf-8")
        
        ids_part = XmlPart(partname, CONTENT_TYPE_IDS, parse_xml(xml_bytes), package)
        package.parts.append(ids_part)
        self.doc.part.relate_to(ids_part, RELTYPE_IDS)
        
        return ids_part

    def _get_or_create_extensible_part(self) -> XmlPart:
        """
        Retrieves or creates the commentsExtensible part.
        Required for Modern Comments metadata (dateUtc via durableId).
        """
        RELTYPE_EXTENSIBLE = "http://schemas.microsoft.com/office/2018/relationships/commentsExtensible"
        CONTENT_TYPE_EXTENSIBLE = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml"
        
        # 1. Check existing
        try:
            for rel in self.doc.part.rels.values():
                if rel.reltype == RELTYPE_EXTENSIBLE:
                    part = rel.target_part
                    if not isinstance(part, XmlPart):
                        xml_part = XmlPart(
                            part.partname, 
                            part.content_type, 
                            parse_xml(part.blob), 
                            part.package
                        )
                        if part in part.package.parts:
                            idx = part.package.parts.index(part)
                            part.package.parts[idx] = xml_part
                        rel._target = xml_part
                        return xml_part
                    return part
        except Exception:
            pass
             
        # 2. Create new if missing
        package = self.doc.part.package
        partname = package.next_partname("/word/commentsExtensible%d.xml")
        
        # Root element <w16cex:commentsExtensible>
        xml_bytes = (f"<w16cex:commentsExtensible {nsdecls('w16cex')}></w16cex:commentsExtensible>").encode("utf-8")
        
        extensible_part = XmlPart(partname, CONTENT_TYPE_EXTENSIBLE, parse_xml(xml_bytes), package)
        package.parts.append(extensible_part)
        self.doc.part.relate_to(extensible_part, RELTYPE_EXTENSIBLE)
        
        return extensible_part

    def _ensure_namespaces(self):
        """
        Ensures 'w14' and 'w15' namespaces are declared on the root <w:comments> element.
        """
        if not self.comments_part:
            return

        element = self.comments_part.element
        has_w14 = "w14" in element.nsmap and element.nsmap["w14"] == w14_ns
        has_w15 = "w15" in element.nsmap and element.nsmap["w15"] == w15_ns
        
        if has_w14 and has_w15:
            return

        xml_str = serialize_for_reading(element)
        
        # Brute force injection into the tag attributes if missing
        # We replace the first occurrence of <w:comments
        if "xmlns:w14=" not in xml_str or "xmlns:w15=" not in xml_str:
            replacement = f'<w:comments xmlns:w14="{w14_ns}" xmlns:w15="{w15_ns}"'
            new_xml = xml_str.replace("<w:comments", replacement, 1)
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
        """Generates a random 8-char hex string for w14:paraId"""
        return f"{random.randint(0, 0xFFFFFFFF):08X}"
        
    def _generate_durable_id(self) -> str:
        """Generates a random 8-char hex string for w16cid:durableId"""
        return f"{random.randint(0, 0xFFFFFFFF):08X}"

    def _find_para_id_for_comment(self, comment_id: str) -> Optional[str]:
        """Finds the w14:paraId of the first paragraph in the given comment ID."""
        if not self.comments_part:
            return None
            
        # Find comment by ID
        # XPath is cleaner but python-docx elements support direct findall
        for c in self.comments_part.element.findall(qn("w:comment")):
            if c.get(qn("w:id")) == comment_id:
                # Find first paragraph
                p = c.find(qn("w:p"))
                if p is not None:
                    return p.get(qn("w14:paraId"))
        return None

    def _add_to_extended_part(self, para_id: str, parent_para_id: Optional[str]):
        """
        Adds a <w15:commentEx> entry to commentsExtended.xml.
        """
        if not self.extended_part:
            return

        # <w15:commentEx w15:paraId="{para_id}" w15:paraIdParent="{parent_para_id}" w15:done="0"/>
        comment_ex = OxmlElement("w15:commentEx")
        comment_ex.set(qn("w15:paraId"), para_id)
        if parent_para_id:
            comment_ex.set(qn("w15:paraIdParent"), parent_para_id)
        comment_ex.set(qn("w15:done"), "0")
        
        self.extended_part.element.append(comment_ex)

    def _add_to_ids_part(self, para_id: str):
        """
        Adds a <w16cid:commentId> entry to commentsIds.xml.
        """
        if not self.ids_part:
            return
        comment_id_el = OxmlElement("w16cid:commentId")
        comment_id_el.set(qn("w16cid:paraId"), para_id)
        comment_id_el.set(qn("w16cid:durableId"), self._generate_durable_id())
        self.ids_part.element.append(comment_id_el)

    def _add_to_extensible_part(self, para_id: str, date_utc: str):
        """
        Adds a <w16cex:commentExtensible> entry to commentsExtensible.xml.
        Finds the durableId from the IDs part first.
        """
        if not self.extensible_part or not self.ids_part:
            return
        
        # We need to find the durableId we just assigned to this paraId
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
        comment_id = str(self.next_id)
        self.next_id += 1

        # Word expects strict ISO 8601 UTC: YYYY-MM-DDThh:mm:ssZ
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

        comment = OxmlElement("w:comment")
        comment.set(qn("w:id"), comment_id)
        comment.set(qn("w:author"), author)
        comment.set(qn("w:date"), now)

        # Legacy Threading (w15:p)
        # We always add this for backward compatibility (and redundancy)
        if parent_id:
            comment.set(qn("w15:p"), str(parent_id))

        # Modern Threading (w14:paraId)
        para_id = self._generate_para_id()
        
        p = OxmlElement("w:p")
        p.set(qn("w14:paraId"), para_id)
        # Also need w14:textId ideally, but ParaId is the structural key
        p.set(qn("w14:textId"), self._generate_para_id()) # Random hex is fine

        # 1. Add Paragraph Style (CommentText)
        pPr = OxmlElement("w:pPr")
        pStyle = OxmlElement("w:pStyle")
        pStyle.set(qn("w:val"), "CommentText")
        pPr.append(pStyle)
        p.append(pPr)

        # 2. Add Annotation Reference (CRITICAL for visibility)
        r_ref = OxmlElement("w:r")
        rPr_ref = OxmlElement("w:rPr")
        rStyle_ref = OxmlElement("w:rStyle")
        rStyle_ref.set(qn("w:val"), "CommentReference")
        rPr_ref.append(rStyle_ref)
        r_ref.append(OxmlElement("w:annotationRef"))
        p.append(r_ref)

        # 3. Add Content Run
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = text

        r.append(t)
        p.append(r)
        comment.append(p)

        self.comments_part.element.append(comment)

        # Handle Extended Part (if it exists)
        if self.extended_part:
            parent_para_id = None
            if parent_id:
                parent_para_id = self._find_para_id_for_comment(parent_id)
                # If we can't find parent paraId, we can't link in modern way.
                # Word might rely on w15:p in that case or show as broken.
                
            self._add_to_extended_part(para_id, parent_para_id)

        # Handle IDs Part
        if self.ids_part:
            self._add_to_ids_part(para_id)

        # Handle Extensible Part
        if self.extensible_part:
            self._add_to_extensible_part(para_id, now)

        return comment_id

    def extract_comments_data(self) -> Dict[str, dict]:
        """
        Parses the comments part and returns a map of:
        {
            "id": { ... }
        }
        """
        data: Dict[str, dict] = {}
        if not self.comments_part:
            return data

        comments = self.comments_part.element.findall(qn("w:comment"))
        for c in comments:
            c_id = c.get(qn("w:id"))
            c_author = c.get(qn("w:author")) or "Unknown"
            c_date = c.get(qn("w:date")) or ""

            # Check for Resolved status (w15:done="1")
            is_resolved = False
            # We can use qn('w15:done') now that it's registered
            val = c.get(qn("w15:done"))
            if val in ("1", "true", "on"):
                is_resolved = True

            # Check for Parent ID (w15:p)
            # Use Clark notation for robust reading to avoid prefix issues during read
            parent_id = c.get("{http://schemas.microsoft.com/office/word/2012/wordml}p")
            
            # TODO: If parent_id is missing here, we *could* try to resolve it via commentsExtended
            # by looking up paraId -> paraIdParent -> parentCommentId.
            # For now, we rely on legacy reading, which serves our current needs.

            # Extract text from all paragraphs within the comment
            text_parts = []
            for p in c.findall(qn("w:p")):
                for r in p.findall(qn("w:r")):
                    for t in r.findall(qn("w:t")):
                        if t.text:
                            text_parts.append(t.text)
                text_parts.append("\n")  # Paragraph break in comment

            full_text = "".join(text_parts).strip()

            data[c_id] = {
                "author": c_author,
                "text": full_text,
                "date": c_date,
                "resolved": is_resolved,
                "parent_id": parent_id,
            }

        return data
