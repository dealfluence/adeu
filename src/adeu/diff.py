from diff_match_patch import diff_match_patch
from typing import List
from adeu.models import ComplianceEdit, EditOperationType
import structlog

logger = structlog.get_logger(__name__)

def generate_edits_from_text(original_text: str, modified_text: str) -> List[ComplianceEdit]:
    """
    Compares original and modified text to generate structured ComplianceEdit objects.
    """
    dmp = diff_match_patch()
    
    # 1. Compute Diff (Semantic Cleanup makes it human-readable)
    diffs = dmp.diff_main(original_text, modified_text)
    dmp.diff_cleanupSemantic(diffs)
    
    edits = []
    
    # We need to track context (previous text) to anchor insertions
    # dmp.diff_main returns list of tuples: (operation, text)
    # 0 = Equal, 1 = Insert, -1 = Delete
    
    last_equal_text = ""
    
    for op, text in diffs:
        if op == 0: # Equal
            # Keep track of the last known "stable" text to use as anchor
            # We only need the last few words to be safe, but storing it all is fine for now
            last_equal_text = text
            
        elif op == -1: # Delete
            # Create Deletion Edit
            edits.append(ComplianceEdit(
                operation=EditOperationType.DELETION,
                target_text_to_change_or_anchor=text,
                proposed_new_text=None,
                thought_process="Diff: Text deleted"
            ))
            
        elif op == 1: # Insert
            # Create Insertion Edit
            # We anchor it to the END of the last_equal_text
            # Note: This is a simplification. A robust engine handles start-of-file inserts.
            
            anchor = last_equal_text[-50:] if last_equal_text else ""
            
            logger.debug(f"Diff Insert: {repr(text)} Anchor (Tail): {repr(anchor[-20:])}")
            # If immediately following a deletion, we might want to merge into MODIFICATION?
            # For this MVP, we treat them as separate atomic ops (Delete X, then Insert Y).
            # The RedlineEngine handles sequential edits fine.
            
            if not anchor:
                logger.warning(f"Insertion at start of file or without context: '{text[:20]}...'")
                # Fallback: We might need a 'prepend' logic in engine, 
                # but for contracts, usually there is context.
                continue

            edits.append(ComplianceEdit(
                operation=EditOperationType.INSERTION,
                target_text_to_change_or_anchor=anchor,
                proposed_new_text=text,
                thought_process="Diff: Text inserted"
            ))
            
    # Optimization: Merge adjacent DELETE + INSERT into MODIFICATION?
    # This helps the engine by giving it a specific target to replace.
    merged_edits = _merge_diffs(edits)
    return merged_edits

def _merge_diffs(edits: List[ComplianceEdit]) -> List[ComplianceEdit]:
    """
    Heuristic: If we see DELETE(A) followed immediately by INSERT(Anchor=PrecedingA, Text=B),
    convert to MODIFICATION(Target=A, New=B).
    """
    merged = []
    i = 0
    while i < len(edits):
        current = edits[i]
        
        # Check if next exists
        if i + 1 < len(edits):
            next_edit = edits[i+1]
            
            # Check for pattern: DELETE then INSERT
            if (current.operation == EditOperationType.DELETION and 
                next_edit.operation == EditOperationType.INSERTION):
                
                # Check if the insertion is conceptually replacing this deletion
                # (Simple heuristic: they happened at the same diff point)
                # Since we iterate linear diffs, this is usually true if adjacent.
                
                merged.append(ComplianceEdit(
                    operation=EditOperationType.MODIFICATION,
                    target_text_to_change_or_anchor=current.target_text_to_change_or_anchor,
                    proposed_new_text=next_edit.proposed_new_text,
                    thought_process="Diff: Replacement"
                ))
                i += 2 # Skip both
                continue
                
        merged.append(current)
        i += 1
        
    return merged