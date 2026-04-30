# Specification: Structural Table Edits

## 1. The Problem
Adeu currently represents tables in a linear virtual text format: 
`Cell 1 | Cell 2 \n Cell 3 | Cell 4`

While `ModifyText` is excellent for search-and-replace within a cell, allowing an LLM to add or remove structural boundaries (`|` or `\n`) via simple string substitution is dangerous. It easily leads to gridspan corruption, misaligned `<w:tc>` elements, and broken OOXML table grids.

## 2. API Expansion Strategy
To solve this, we are expanding the `DocumentChange` discriminated union with explicit structural table operations. This forces the LLM to declare its *intent* to modify the grid, allowing the Redline Engine to execute safe, atomic XML operations on the `python-docx` Table objects.

### 2.1 Proposed New Schema Models

```python
class InsertTableRow(BaseModel):
    type: Literal["insert_row"] = Field("insert_row")
    
    target_text: str = Field(
        ..., 
        description="Text inside an existing row to use as an anchor. The new row will be inserted relative to this row."
    )
    
    position: Literal["above", "below"] = Field(
        "below", 
        description="Whether to insert the new row above or below the anchor row."
    )
    
    cells: List[str] = Field(
        ..., 
        description="A list of Markdown strings representing the contents of the new cells."
    )

class DeleteTableRow(BaseModel):
    type: Literal["delete_row"] = Field("delete_row")
    
    target_text: str = Field(
        ..., 
        description="Text inside the row you wish to delete. The engine will delete the entire row containing this match."
    )
```

*(Note: Similar models like `InsertTableColumn` and `DeleteTableColumn` can be added, but row operations are significantly safer and more common in contract redlining.)*

## 3. Redline Engine Execution Rules

When the `RedlineEngine` processes these new operations, it must adhere to the following strict safety rules:

1. **Resolution via Mapper:** 
   - The engine uses `mapper.find_target_runs(target_text)` to find the anchor text.
   - It traverses up the DOM (`run._element.getparent()`) until it finds the `<w:tr>` (Table Row) and `<w:tbl>` (Table) elements.
   
2. **Tracked Changes Compatibility:**
   - Word natively supports tracked table row insertions (`<w:trPr><w:ins .../></w:trPr>`) and deletions (`<w:trPr><w:del .../></w:trPr>`).
   - The engine MUST inject these revision tags into the row properties to ensure the structural change appears as a redline in the Review pane, rather than a silent overwrite.

3. **Column Constraints (The OOXML Grid Issue):**
   - Microsoft Word relies on a `<w:tblGrid>` definition at the top of the table. Dynamically inserting a column requires recalculating cell widths, inserting `<w:gridCol>` definitions, and safely injecting `<w:tc>` into every single `<w:tr>` across the table. 
   - **Recommendation for v1.4.2:** Ship *Row* operations first. Defer *Column* operations unless strictly requested by users, as column modifications in OOXML are heavily prone to document corruption.
