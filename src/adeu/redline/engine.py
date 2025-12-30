import datetime
from copy import deepcopy
from io import BytesIO
from typing import List

import structlog
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.run import Run

from adeu.models import ComplianceEdit, EditOperationType
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import normalize_docx, create_element, create_attribute

logger = structlog.get_logger(__name__)

class RedlineEngine:
    def __init__(self, doc_stream: BytesIO):
        self.doc = Document(doc_stream)
        
        # 1. Normalize immediately to make mapping easier
        normalize_docx(self.doc)
        
        self.author = "Adeu AI"
        self.timestamp = (
            datetime.datetime.now().replace(microsecond=0).isoformat() + "Z"
        )
        self.current_id = 0
        
        # Initialize mapper
        self.mapper = DocumentMapper(self.doc)

    def _get_next_id(self):
        self.current_id += 1
        return str(self.current_id)

    def _create_track_change_tag(self, tag_name: str, author: str = ""):
        tag = create_element(tag_name)
        create_attribute(tag, "w:id", self._get_next_id())
        create_attribute(tag, "w:author", author or self.author)
        create_attribute(tag, "w:date", self.timestamp)
        return tag

    def _set_text_content(self, element, text: str):
        element.text = text
        if text.strip() != text:
            create_attribute(element, "xml:space", "preserve")

    def track_insert(self, text: str, anchor_run: Run = None):
        """
        Creates a w:ins element.
        If anchor_run is provided, copies its formatting properties.
        """
        ins = self._create_track_change_tag("w:ins")
        run = create_element("w:r")
        
        # Copy formatting from anchor if available
        if anchor_run and anchor_run._element.rPr is not None:
            run.append(deepcopy(anchor_run._element.rPr))
            
        t = create_element("w:t")
        self._set_text_content(t, text)
        run.append(t)
        ins.append(run)
        return ins

    def track_delete_run(self, run: Run):
        del_tag = self._create_track_change_tag("w:del")
        new_run = create_element("w:r")

        if run._r.rPr is not None:
            new_run.append(deepcopy(run._r.rPr))

        text_content = run.text
        del_text = create_element("w:delText")
        self._set_text_content(del_text, text_content)

        new_run.append(del_text)
        del_tag.append(new_run)

        run._r.getparent().replace(run._r, del_tag)
        return del_tag

    def apply_edits(self, edits: List[ComplianceEdit]):
        """
        Applies a list of ComplianceEdits to the document.
        Sorts edits by length (descending) to minimize index shift collisions
        (though Mapper rebuilds help).
        """
        for e in edits:
            logger.debug(f"Edit: {e.operation} '{repr(e.target_text_to_change_or_anchor)}' -> '{repr(e.proposed_new_text)}'")
    
        # Sort by length of target text to handle specific matches before general ones
        sorted_edits = sorted(
            edits, 
            key=lambda x: len(x.target_text_to_change_or_anchor), 
            reverse=True
        )
        
        logger.info(f"Applying {len(sorted_edits)} edits.")

        for edit in sorted_edits:
            self._apply_single_edit(edit)
            
    def _apply_single_edit(self, edit: ComplianceEdit):
        target_text = edit.target_text_to_change_or_anchor
        target_runs = self.mapper.find_target_runs(target_text)

        if not target_runs:
            logger.warning(f"Skipping edit: Target '{target_text[:20]}...' not found.")
            return
            
        # Debug: Capture XML context
        parent_p = target_runs[0]._element.getparent()
        while parent_p.tag != qn("w:p") and parent_p.getparent() is not None:
            parent_p = parent_p.getparent()
            
        before_xml = parent_p.xml

        if edit.operation == EditOperationType.DELETION:
            for run in target_runs:
                self.track_delete_run(run)

        elif edit.operation == EditOperationType.MODIFICATION:
            if not edit.proposed_new_text:
                return

            last_del_element = None
            # Delete old text
            for run in target_runs:
                last_del_element = self.track_delete_run(run)

            # Insert new text after the last deletion
            if last_del_element is not None:
                parent = last_del_element.getparent()
                del_index = parent.index(last_del_element)
                
                # Use the last run as the style anchor
                ins_elem = self.track_insert(
                    edit.proposed_new_text, 
                    anchor_run=Run(target_runs[-1]._element, None) # Wraps the deleted element essentially
                )
                parent.insert(del_index + 1, ins_elem)

        elif edit.operation == EditOperationType.INSERTION:
            if not edit.proposed_new_text:
                return

            last_run = target_runs[-1]
            parent = last_run._r.getparent()
            index = parent.index(last_run._r)

            # Do NOT automatically prepend space. Trust the Diff engine.
            ins_elem = self.track_insert(edit.proposed_new_text, anchor_run=last_run)
            parent.insert(index + 1, ins_elem)
            
        # Debug: Log change
        after_xml = parent_p.xml
        logger.debug(f"XML Change for '{edit.proposed_new_text or 'DEL'}':\nBEFORE: {before_xml}\nAFTER:  {after_xml}")

    def save_to_stream(self) -> BytesIO:
        output = BytesIO()
        self.doc.save(output)
        output.seek(0)
        return output