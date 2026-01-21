import datetime
from typing import Dict

from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.part import XmlPart
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn


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
            for rel in self.doc.part.rels.values():
                if rel.reltype == RT.COMMENTS:
                    return rel.target_part
        except Exception:
            pass

        # 2. Create new part if not found
        package = self.doc.part.package
        partname = package.next_partname("/word/comments%d.xml")
        content_type = CT.WML_COMMENTS

        xml_bytes = (f"<w:comments {nsdecls('w')}>\n</w:comments>").encode("utf-8")

        comments_part = XmlPart(partname, content_type, parse_xml(xml_bytes), package)
        package.parts.append(comments_part)
        self.doc.part.relate_to(comments_part, RT.COMMENTS)

        return comments_part

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

    def add_comment(self, author: str, text: str) -> str:
        comment_id = str(self.next_id)
        self.next_id += 1

        now = datetime.datetime.now().isoformat()

        comment = OxmlElement("w:comment")
        comment.set(qn("w:id"), comment_id)
        comment.set(qn("w:author"), author)
        comment.set(qn("w:date"), now)

        p = OxmlElement("w:p")
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = text

        r.append(t)
        p.append(r)
        comment.append(p)

        self.comments_part.element.append(comment)

        return comment_id

    def extract_comments_data(self) -> Dict[str, dict]:
        """
        Parses the comments part and returns a map of:
        {
            "id": {
                "author": str,
                "text": str,
                "date": str,
                "resolved": bool
            }
        }
        """
        data: Dict[str, dict] = {}
        if not self.comments_part:
            return data

        # Register w15 namespace for 'done' (Resolved) attribute
        w15_ns = "http://schemas.microsoft.com/office/word/2012/wordml"

        comments = self.comments_part.element.findall(qn("w:comment"))
        for c in comments:
            c_id = c.get(qn("w:id"))
            c_author = c.get(qn("w:author")) or "Unknown"
            c_date = c.get(qn("w:date")) or ""

            # Check for Resolved status (w15:done="1")
            is_resolved = False
            val = c.get(f"{{{w15_ns}}}done")
            if val in ("1", "true", "on"):
                is_resolved = True

            # Extract text from all paragraphs within the comment
            text_parts = []
            for p in c.findall(qn("w:p")):
                for r in p.findall(qn("w:r")):
                    for t in r.findall(qn("w:t")):
                        if t.text:
                            text_parts.append(t.text)
                text_parts.append("\n")  # Paragraph break in comment

            full_text = "".join(text_parts).strip()

            data[c_id] = {"author": c_author, "text": full_text, "date": c_date, "resolved": is_resolved}

        return data
