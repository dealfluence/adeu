import datetime
from typing import Optional

from docx.opc.constants import CONTENT_TYPE as CT, RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml, OxmlElement
from docx.oxml.ns import nsdecls, qn
from docx.opc.part import XmlPart

class CommentsManager:
    """
    Manages the 'word/comments.xml' part of the DOCX package.
    Handles creation of the part if it doesn't exist and adding comments.
    """
    def __init__(self, doc):
        self.doc = doc
        self.comments_part = self._get_or_create_comments_part()
        self.next_id = self._get_next_comment_id()

    def _get_or_create_comments_part(self):
        """
        Retrieves the existing comments part or creates a new one 
        linked to the main document part.
        """
        # 1. Check if comments part exists via relationships
        try:
            part = self.doc.part.package.image_parts._parts[-1] # Hacky access to parts? No.
            # Standard access via relationships
            for rel in self.doc.part.rels.values():
                if rel.reltype == RT.COMMENTS:
                    return rel.target_part
        except Exception:
            pass

        # 2. Check internal lists if python-docx loaded it but didn't expose it easily
        # (Usually handled by rels above). 

        # 3. Create new part if not found
        # We need to construct a new XmlPart and register it.
        # This is complex in python-docx without using internal methods.
        # We try to use the public API to add a part if possible, 
        # but often we must rely on the fact that python-docx loads comments 
        # as a Part if present.
        
        # If we are here, we likely need to CREATE it.
        # Creating a part from scratch using python-docx internals:
        package = self.doc.part.package
        partname = package.next_partname("/word/comments%d.xml")
        content_type = CT.WML_COMMENTS
        
        # Create the XML body
        xml_bytes = (
            f'<w:comments {nsdecls("w")}>\n'
            f'</w:comments>'
        ).encode("utf-8")
        
        # Use BaseStoryPart or generic Part. 
        # We can instantiate a generic XmlPart.
        comments_part = XmlPart(partname, content_type, parse_xml(xml_bytes), package)
        
        # Add to package
        package.parts.append(comments_part)
        
        # Add relationship from Document to Comments
        self.doc.part.relate_to(comments_part, RT.COMMENTS)
        
        return comments_part

    def _get_next_comment_id(self) -> int:
        """Finds the next available ID by scanning existing comments."""
        ids = [0]
        comments = self.comments_part.element.findall(qn("w:comment"))
        for c in comments:
            try:
                ids.append(int(c.get(qn("w:id"))))
            except (ValueError, TypeError):
                pass
        return max(ids) + 1

    def add_comment(self, author: str, text: str) -> str:
        """
        Adds a <w:comment> element to comments.xml and returns its ID.
        """
        comment_id = str(self.next_id)
        self.next_id += 1
        
        now = datetime.datetime.now().isoformat()
        
        # Construct the w:comment element
        comment = OxmlElement("w:comment")
        comment.set(qn("w:id"), comment_id)
        comment.set(qn("w:author"), author)
        comment.set(qn("w:date"), now)
        # Initials are optional, skipping for now
        
        p = OxmlElement("w:p")
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = text
        
        r.append(t)
        p.append(r)
        comment.append(p)
        
        self.comments_part.element.append(comment)
        
        return comment_id