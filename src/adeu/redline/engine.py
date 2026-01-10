import datetime
from copy import deepcopy
from io import BytesIO
from typing import List, Optional

import structlog
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.run import Run

from adeu.models import DocumentEdit, EditOperationType
from adeu.redline.comments import CommentsManager
from adeu.redline.mapper import DocumentMapper
from adeu.utils.docx import normalize_docx, create_element, create_attribute

logger = structlog.get_logger(__name__)

class RedlineEngine:
    def __init__(self, doc_stream: BytesIO, author: str = "Adeu AI"):
        self.doc = Document(doc_stream)
        
        # 1. Normalize immediately to make mapping easier
        normalize_docx(self.doc)
        
        self.author = author
        self.timestamp = (
            datetime.datetime.now().replace(microsecond=0).isoformat() + "Z"
        )
        self.current_id = 0
        
        # Initialize mapper
        self.mapper = DocumentMapper(self.doc)
        
        # Initialize Comments Manager
        self.comments_manager = CommentsManager(self.doc)

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

        parent = run._r.getparent()
        if parent is None:
            logger.warning(f"Attempted to delete run '{run.text}' but it is detached from DOM.")
            return None

        parent.replace(run._r, del_tag)
        return del_tag

    def _attach_comment(self, parent_element, start_element, end_element, text: str):
        """
        Wraps the range [start_element, end_element] with w:commentRangeStart/End
        and appends a w:commentReference.
        """
        if not text:
            return

        # 1. Create Comment in comments.xml
        comment_id = self.comments_manager.add_comment(self.author, text)

        # 2. Create Anchors
        range_start = create_element("w:commentRangeStart")
        create_attribute(range_start, "w:id", comment_id)

        range_end = create_element("w:commentRangeEnd")
        create_attribute(range_end, "w:id", comment_id)

        # 3. Create Reference (Visual Bubble)
        # Usually placed immediately after the range end
        ref_run = create_element("w:r")
        ref = create_element("w:commentReference")
        create_attribute(ref, "w:id", comment_id)
        ref_run.append(ref)

        # 4. Insert into DOM
        # Insert Start before start_element
        start_index = parent_element.index(start_element)
        parent_element.insert(start_index, range_start)

        # Insert End after end_element (account for shift by start tag)
        # If start==end (single element), index is now start_index + 1
        end_index = parent_element.index(end_element)
        parent_element.insert(end_index + 1, range_end)

        # Insert Reference after End
        parent_element.insert(end_index + 2, ref_run)

    def apply_edits(self, edits: List[DocumentEdit]) -> tuple[int, int]:
        """
        Applies a list of ComplianceEdits to the document.
        Sorts indexed edits DESCENDING to prevent index shifting.
        Returns (applied_count, skipped_count).
        """
        indexed_edits = [e for e in edits if e._match_start_index is not None]
        unindexed_edits = [e for e in edits if e._match_start_index is None]
        
        applied = 0
        skipped = 0
        
        # 1. Apply Indexed Edits (Reverse Order)
        indexed_edits.sort(key=lambda x: x._match_start_index, reverse=True)
        
        logger.info(f"Applying {len(indexed_edits)} indexed edits (Reverse Order).")
        for edit in indexed_edits:
            if self._apply_single_edit_indexed(edit):
                applied += 1
            else:
                skipped += 1

        # 2. Apply Unindexed Edits (Heuristic Fallback)
        if unindexed_edits:
            unindexed_edits.sort(key=lambda x: len(x.target_text), reverse=True)
            logger.info(f"Applying {len(unindexed_edits)} unindexed edits (Heuristic).")
            # Rebuild map as safety measure
            self.mapper._build_map()
            for edit in unindexed_edits:
                if self._apply_single_edit_heuristic(edit):
                    applied += 1
                    # IMPORTANT: DOM has changed. Rebuild map so next edits 
                    # find the correct runs (and don't find deleted ones).
                    self.mapper._build_map()
                else:
                    skipped += 1
        return applied, skipped

    def _apply_single_edit_heuristic(self, edit: DocumentEdit) -> bool:
        """Legacy logic using string search"""
        target_text = edit.target_text
        target_runs = self.mapper.find_target_runs(target_text)

        if not target_runs:
            logger.warning(f"Skipping edit: Target '{target_text[:20]}...' not found.")
            return False
            
        logger.debug(f"Target runs found: {len(target_runs)}. Last run text: '{target_runs[-1].text}'")
        
        if edit.operation == EditOperationType.DELETION:
            for run in target_runs:
                self.track_delete_run(run)

        elif edit.operation == EditOperationType.MODIFICATION:
            if not edit.new_text:
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
                    edit.new_text, 
                    anchor_run=Run(target_runs[-1]._element, None) # Wraps the deleted element essentially
                )
                parent.insert(del_index + 1, ins_elem)
                
                # Insert Comment if present
                if edit.comment:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)

        elif edit.operation == EditOperationType.INSERTION:
            if not edit.new_text:
                return

            last_run = target_runs[-1]
            parent = last_run._r.getparent()
            index = parent.index(last_run._r)
            
            # Determine style source
            next_run = self._get_next_run(last_run)
            style_run = self._determine_style_source(
                prev_run=last_run, 
                next_run=next_run, 
                insert_text=edit.new_text
            )
            
            logger.debug(f"Insert Index: {index+1}. Parent len: {len(parent)}")

            # Do NOT automatically prepend space. Trust the Diff engine.
            ins_elem = self.track_insert(edit.new_text, anchor_run=style_run)
            parent.insert(index + 1, ins_elem)
            
            # Insert Comment if present
            if edit.comment:
                self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
        return True

    def _apply_single_edit_indexed(self, edit: DocumentEdit) -> bool:
        """
        Applies edit using exact index coordinates.
        """
        start_idx = edit._match_start_index
        target_text = edit.target_text
        length = len(target_text) if target_text else 0
        
        logger.debug(f"Applying Edit at [{start_idx}:{start_idx+length}] Op={edit.operation}")

        if edit.operation == EditOperationType.INSERTION:
            anchor_run = self.mapper.get_insertion_anchor(start_idx)
            if not anchor_run:
                logger.warning(f"Could not find anchor for insertion at {start_idx}")
                return False
            
            parent = anchor_run._element.getparent()
            index = parent.index(anchor_run._element)
            
            # Special case: Insert At Start (Index 0)
            if start_idx == 0:
                 ins_elem = self.track_insert(edit.new_text, anchor_run=anchor_run)
                 parent.insert(index, ins_elem)
                 
                 if edit.comment:
                     self._attach_comment(parent, ins_elem, ins_elem, edit.comment)

            else:
                 # Resolve style inheritance (Prev vs Next)
                 next_run = self._get_next_run(anchor_run)
                 style_run = self._determine_style_source(
                     prev_run=anchor_run,
                     next_run=next_run,
                     insert_text=edit.new_text
                 )
                 ins_elem = self.track_insert(edit.new_text, anchor_run=style_run)
                 parent.insert(index + 1, ins_elem)
                 
                 # Insert Comment
                 if edit.comment:
                     self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
            return True

        # For DEL/MOD, we need to identify the runs to delete
        target_runs = self.mapper.find_target_runs_by_index(start_idx, length)
        
        if not target_runs:
             logger.warning(f"Target runs not found for index {start_idx}")
             return False

        if edit.operation == EditOperationType.DELETION:
            for run in target_runs:
                self.track_delete_run(run)

        elif edit.operation == EditOperationType.MODIFICATION:
            last_del_element = None
            for run in target_runs:
                last_del_element = self.track_delete_run(run)
            
            if last_del_element is not None and edit.new_text:
                parent = last_del_element.getparent()
                del_index = parent.index(last_del_element)
                
                ins_elem = self.track_insert(
                    edit.new_text, 
                    anchor_run=Run(target_runs[-1]._element, None) 
                )
                parent.insert(del_index + 1, ins_elem)
                
                # Insert Comment
                if edit.comment:
                    self._attach_comment(parent, ins_elem, ins_elem, edit.comment)
        return True

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