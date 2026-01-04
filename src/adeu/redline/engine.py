import datetime
from copy import deepcopy
from io import BytesIO
from typing import List, Optional

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
        Sorts indexed edits DESCENDING to prevent index shifting.
        """
        indexed_edits = [e for e in edits if e.match_start_index is not None]
        unindexed_edits = [e for e in edits if e.match_start_index is None]
        
        # 1. Apply Indexed Edits (Reverse Order)
        indexed_edits.sort(key=lambda x: x.match_start_index, reverse=True)
        
        logger.info(f"Applying {len(indexed_edits)} indexed edits (Reverse Order).")
        for edit in indexed_edits:
            self._apply_single_edit_indexed(edit)

        # 2. Apply Unindexed Edits (Heuristic Fallback)
        if unindexed_edits:
            unindexed_edits.sort(key=lambda x: len(x.target_text_to_change_or_anchor), reverse=True)
            logger.info(f"Applying {len(unindexed_edits)} unindexed edits (Heuristic).")
            # Rebuild map as safety measure
            self.mapper._build_map()
            for edit in unindexed_edits:
                self._apply_single_edit_heuristic(edit)

    def _apply_single_edit_heuristic(self, edit: ComplianceEdit):
        """Legacy logic using string search"""
        target_text = edit.target_text_to_change_or_anchor
        target_runs = self.mapper.find_target_runs(target_text)

        if not target_runs:
            logger.warning(f"Skipping edit: Target '{target_text[:20]}...' not found.")
            return
            
        logger.debug(f"Target runs found: {len(target_runs)}. Last run text: '{target_runs[-1].text}'")
            
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
                
                # Debug context
                if del_index + 1 < len(parent):
                     next_el = parent[del_index+1]
                     logger.debug(f"Inserting modification before element: {next_el.text if hasattr(next_el, 'text') else next_el.tag}")
                
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
            
            # Determine style source
            next_run = self._get_next_run(last_run)
            style_run = self._determine_style_source(
                prev_run=last_run, 
                next_run=next_run, 
                insert_text=edit.proposed_new_text
            )
            
            logger.debug(f"Insert Index: {index+1}. Parent len: {len(parent)}")

            # Do NOT automatically prepend space. Trust the Diff engine.
            ins_elem = self.track_insert(edit.proposed_new_text, anchor_run=style_run)
            parent.insert(index + 1, ins_elem)

    def _apply_single_edit_indexed(self, edit: ComplianceEdit):
        """
        Applies edit using exact index coordinates.
        """
        start_idx = edit.match_start_index
        target_text = edit.target_text_to_change_or_anchor
        length = len(target_text) if target_text else 0
        
        logger.debug(f"Applying Edit at [{start_idx}:{start_idx+length}] Op={edit.operation}")

        if edit.operation == EditOperationType.INSERTION:
            anchor_run = self.mapper.get_insertion_anchor(start_idx)
            if not anchor_run:
                logger.warning(f"Could not find anchor for insertion at {start_idx}")
                return
            
            parent = anchor_run._element.getparent()
            index = parent.index(anchor_run._element)
            
            # Special case: Insert At Start (Index 0)
            if start_idx == 0:
                 ins_elem = self.track_insert(edit.proposed_new_text, anchor_run=anchor_run)
                 parent.insert(index, ins_elem)
            else:
                 # Resolve style inheritance (Prev vs Next)
                 next_run = self._get_next_run(anchor_run)
                 style_run = self._determine_style_source(
                     prev_run=anchor_run,
                     next_run=next_run,
                     insert_text=edit.proposed_new_text
                 )
                 ins_elem = self.track_insert(edit.proposed_new_text, anchor_run=style_run)
                 parent.insert(index + 1, ins_elem)
            return

        # For DEL/MOD, we need to identify the runs to delete
        target_runs = self.mapper.find_target_runs_by_index(start_idx, length)
        
        if not target_runs:
             logger.warning(f"Target runs not found for index {start_idx}")
             return

        if edit.operation == EditOperationType.DELETION:
            for run in target_runs:
                self.track_delete_run(run)

        elif edit.operation == EditOperationType.MODIFICATION:
            last_del_element = None
            for run in target_runs:
                last_del_element = self.track_delete_run(run)
            
            if last_del_element is not None and edit.proposed_new_text:
                parent = last_del_element.getparent()
                del_index = parent.index(last_del_element)
                
                ins_elem = self.track_insert(
                    edit.proposed_new_text, 
                    anchor_run=Run(target_runs[-1]._element, None) 
                )
                parent.insert(del_index + 1, ins_elem)

    def _get_next_run(self, run: Run) -> Optional[Run]:
        """
        Returns the next sibling Run element, skipping non-run elements.
        """
        curr = run._element
        while True:
            curr = curr.getnext()
            if curr is None:
                return None
            if curr.tag == qn("w:r"):
                return Run(curr, run._parent)

    def _determine_style_source(self, prev_run: Run, next_run: Optional[Run], insert_text: str) -> Run:
        """
        Heuristic to decide whether insertion should inherit style from 
        the previous run (standard) or the next run (e.g. prepending to a bold sentence).
        """
        if not next_run:
            return prev_run
            
        # Heuristic 1: If text ends with space (e.g. "Very "), it is likely an adjective/start 
        # of the NEXT phrase. Inherit NEXT style.
        if insert_text and insert_text.endswith(" "):
            return next_run
            
        # Default: Inherit from PREV (standard typing behavior)
        return prev_run

    def save_to_stream(self) -> BytesIO:
        output = BytesIO()
        self.doc.save(output)
        output.seek(0)
        return output