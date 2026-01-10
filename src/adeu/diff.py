import re
from diff_match_patch import diff_match_patch
from typing import List, Dict, Tuple
from adeu.models import DocumentEdit, EditOperationType
import structlog

logger = structlog.get_logger(__name__)

def generate_edits_from_text(original_text: str, modified_text: str) -> List[DocumentEdit]:
    """
    Compares original and modified text to generate structured ComplianceEdit objects.
    Uses Word-Level diffing to ensure natural, readable redlines.
    """
    dmp = diff_match_patch()
    
    # 1. Word-Level Tokenization & Encoding
    # We convert words to single characters so DMP treats them as atomic units.
    chars1, chars2, token_array = _words_to_chars(original_text, modified_text)
    
    # 2. Compute Diff on the Encoded Strings
    # This finds the shortest edit path in terms of *words*, not characters.
    diffs_encoded = dmp.diff_main(chars1, chars2, False)
    
    # 3. Semantic Cleanup (On Tokens)
    # Perform cleanup while still in token-space to group adjacent word edits
    # without breaking word boundaries (e.g. "Solutions" vs "Services").
    dmp.diff_cleanupSemantic(diffs_encoded)

    # 4. Decode back to Text
    # Maps the Unicode characters back to the original word tokens.
    dmp.diff_charsToLines(diffs_encoded, token_array)
    diffs = diffs_encoded # diffs_encoded is modified in-place by charsToLines
    
    edits = []
    
    # Track the cursor position in the ORIGINAL text
    current_original_index = 0
    
    for i, (op, text) in enumerate(diffs):
        if op == 0: # Equal
            # Move cursor forward
            current_original_index += len(text)
            
        elif op == -1: # Delete
            # Create Deletion Edit
            edit = DocumentEdit(
                operation=EditOperationType.DELETION,
                target_text=text,
                new_text=None,
                comment="Diff: Text deleted"
            )
            edit._match_start_index = current_original_index
            edits.append(edit)
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
                        mod_edit = DocumentEdit(
                            operation=EditOperationType.MODIFICATION,
                            target_text=anchor_target,
                            new_text=text + anchor_target,
                            comment="Diff: Start-of-doc insertion (converted to modification)"
                        )
                        mod_edit._match_start_index = current_original_index
                        edits.append(mod_edit)
                        current_original_index += len(anchor_target) # Important!
                        continue

            ins_edit = DocumentEdit(
                operation=EditOperationType.INSERTION,
                target_text=anchor,
                new_text=text,
                comment="Diff: Text inserted"
            )
            ins_edit._match_start_index = current_original_index
            edits.append(ins_edit)
            # Do NOT move original cursor (this text was never in original)
            
    # Optimization: Merge adjacent DELETE + INSERT into MODIFICATION?
    # This helps the engine by giving it a specific target to replace.
    merged_edits = _merge_diffs(edits)
    return merged_edits

def _merge_diffs(edits: List[DocumentEdit]) -> List[DocumentEdit]:
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
                
                mod_edit = DocumentEdit(
                    operation=EditOperationType.MODIFICATION,
                    target_text=current.target_text,
                    new_text=next_edit.new_text,
                    comment="Diff: Replacement"
                )
                mod_edit._match_start_index = current._match_start_index
                merged.append(mod_edit)
                i += 2 # Skip both
                continue
                
        merged.append(current)
        i += 1
        
    return merged

def _words_to_chars(text1: str, text2: str) -> Tuple[str, str, List[str]]:
    """
    Splits text into words/tokens and encodes them as unique Unicode characters.
    This mimics diff_match_patch.diff_linesToChars but for words.
    """
    token_array = []  # Maps char_code -> token_string
    token_hash = {}   # Maps token_string -> char_code
    
    # Regex to split: (Whitespace | Word chars | Non-word/Non-space)
    # This ensures "Word," -> ["Word", ","]
    # We use a capturing group () to keep the delimiters in the list
    split_pattern = r'(\s+|\w+|[^\w\s])'
    
    def encode_text(text: str) -> str:
        # filter(None, ...) removes empty strings resulting from the split
        tokens = [t for t in re.split(split_pattern, text) if t]
        
        encoded_chars = []
        for token in tokens:
            if token in token_hash:
                encoded_chars.append(chr(token_hash[token]))
            else:
                # Assign new code
                # We start a bit higher than 0 to avoid control char issues if any
                code = len(token_array)
                token_hash[token] = code
                token_array.append(token)
                encoded_chars.append(chr(code))
                
        return "".join(encoded_chars)

    chars1 = encode_text(text1)
    chars2 = encode_text(text2)
    return chars1, chars2, token_array