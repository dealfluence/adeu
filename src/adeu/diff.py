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
    
    # Track the cursor position in the ORIGINAL text
    current_original_index = 0
    
    for i, (op, text) in enumerate(diffs):
        if op == 0: # Equal
            # Move cursor forward
            current_original_index += len(text)
            
        elif op == -1: # Delete
            # Create Deletion Edit
            edits.append(ComplianceEdit(
                operation=EditOperationType.DELETION,
                target_text_to_change_or_anchor=text,
                proposed_new_text=None,
                thought_process="Diff: Text deleted",
                match_start_index=current_original_index
            ))
            # Move cursor forward because this text WAS in the original
            current_original_index += len(text)
            
        elif op == 1: # Insert
            # Create Insertion Edit
            # For insertions, target_text is usually the anchor.
            # But if we use indices, we can leave target_text empty or symbolic.
            # We maintain the "Anchor" semantic for fallback/debug.
            
            anchor_start = max(0, current_original_index - 50)
            anchor = original_text[anchor_start:current_original_index]
            
            # Legacy Start-of-Document Logic (fallback)
            if not anchor and current_original_index == 0:
                # Handle Start-of-Document Insertion
                # Look ahead for context
                if i + 1 < len(diffs) and diffs[i+1][0] == 0:
                    next_text = diffs[i+1][1]
                    # Use the start of the next text as the target to modify
                    # Heuristic: Take the first significant word/chunk
                    anchor_target = next_text.split(" ")[0] if " " in next_text else next_text[:20]
                    
                    if anchor_target:
                        logger.info(f"Converting start-of-doc insert to modification of '{anchor_target}'")
                        edits.append(ComplianceEdit(
                            operation=EditOperationType.MODIFICATION,
                            target_text_to_change_or_anchor=anchor_target,
                            proposed_new_text=text + anchor_target,
                            thought_process="Diff: Start-of-doc insertion (converted to modification)",
                            match_start_index=current_original_index
                        ))
                        current_original_index += len(anchor_target) # Important!
                        continue

            edits.append(ComplianceEdit(
                operation=EditOperationType.INSERTION,
                target_text_to_change_or_anchor=anchor,
                proposed_new_text=text,
                thought_process="Diff: Text inserted",
                match_start_index=current_original_index
            ))
            # Do NOT move original cursor (this text was never in original)
            
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
                    thought_process="Diff: Replacement",
                    match_start_index=current.match_start_index # Use start of deletion
                ))
                i += 2 # Skip both
                continue
                
        merged.append(current)
        i += 1
        
    return merged