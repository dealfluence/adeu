# FILE: adeu/docs/spec-undo-record.md
# Specification: Live Word COM UndoRecord Integration

## 1. The Problem
Adeu's `LiveWordEngine` processes a batch of LLM edits via native COM operations on an active Microsoft Word document. A single logical `ModifyText` operation might involve several atomic COM steps to ensure structural safety, such as:
1. Inserting a "Sacrificial X" character.
2. Inserting new text blocks.
3. Attaching/rescuing comments.
4. Deleting the original text.

Currently, Microsoft Word registers each of these granular COM API calls as an individual entry in the user's Undo stack. If an agent executes a batch of 10 edits, it might generate 40+ undo steps. 

If the human user sees the agent's work and presses `Ctrl+Z` (Undo), Word will only revert the *very last* micro-operation (e.g., deleting a Sacrificial X), leaving the document in a completely mangled, intermediate state. The user would have to hold down `Ctrl+Z` dozens of times to revert the agent's action.

## 2. The Solution: `Word.Application.UndoRecord`
We will implement an `UndoRecord` context manager that wraps the entire `process_active_word_batch_core` execution block. 

The `UndoRecord` object (introduced in Word 2010) allows external COM automation to group an arbitrary sequence of document mutations into a single, named transaction in the Word UI. 

### 2.1 Expected UX
When the Adeu Agent executes a batch of edits, the Word Undo dropdown will display a single, consolidated entry (e.g., **"Adeu: Agent Batch Edit"**). The human user can press `Ctrl+Z` exactly once to cleanly revert the entire AI interaction.

## 3. Implementation Design

### 3.1 The Context Manager
We will introduce a safe context manager in `src/adeu/mcp_components/tools/live_word_ops.py` (or similar) to handle the lifecycle of the UndoRecord.

```python
import contextlib
import structlog

logger = structlog.get_logger(__name__)

@contextlib.contextmanager
def managed_undo_record(app, record_name: str):
    """
    Wraps a block of COM mutations in a single Word UndoRecord.
    Degrades gracefully on Word 2007 or earlier.
    """
    record = None
    try:
        record = app.UndoRecord
        # Clean up stale undo record from a previous crash/interrupted session
        if record.IsRecordingCustomRecord:
            try:
                record.EndCustomRecord()
            except Exception:
                pass
        
        # Word limits UndoRecord names to 64 characters
        record.StartCustomRecord(record_name[:64])
    except Exception as e:
        logger.debug(f"UndoRecord initialization bypassed (likely unsupported Word version): {e}")
        record = None  
        
    try:
        yield
    finally:
        if record is not None:
            try:
                record.EndCustomRecord()
            except Exception as e:
                logger.warning(f"Failed to cleanly end UndoRecord: {e}")
```

### 3.2 Integration Point
The context manager will be applied at the highest synchronous level of the live batch processing execution, specifically wrapping the `for change in changes:` loop inside `_process_active_word_batch_core` in `src/adeu/mcp_components/tools/live_word.py`.

```python
def _process_active_word_batch_core(
    changes: List[DocumentChange], author_name: str, file_path: Optional[str] = None
) -> dict[str, Any]:
    
    # ... COM Initialization and Setup ...

    with managed_undo_record(app, "Adeu: Agent Batch Edit"):
        for change in changes:
            try:
                # ... Execute ModifyText, AcceptChange, RejectChange ...
            except Exception as e:
                # ... Error handling ...
                
    # ... Teardown ...
```

## 4. Safety Constraints & Edge Cases
1. **Graceful Degradation**: `app.UndoRecord` will throw an AttributeError on Word 2007 or older. The context manager must catch this and yield silently so the engine continues to function (albeit without grouped undos).
2. **Stale Record Cleanup**: If an MCP tool execution previously crashed mid-batch (e.g., due to a `BatchValidationError` or an unhandled COM exception), Word might be stuck in a state where `IsRecordingCustomRecord` is permanently `True`. The context manager MUST check for and explicitly close any stale records before starting a new one, or Word will crash.
3. **Save Operations**: Calling `doc.Save()` natively breaks the Undo stack in certain configurations of Word. The UndoRecord should strictly encompass mutations, not File I/O.
