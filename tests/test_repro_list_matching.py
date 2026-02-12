# FILE: tests/test_repro_list_matching.py

from adeu.markup import apply_edits_to_markdown
from adeu.models import DocumentEdit


def test_repro_failed_list_match():
    """
    Reproduction of issue where multi-line target text with stripped list markers
    fails to match source text containing bullets (*), bolding (**), and indentation.
    """
    # The source markdown provided in the issue
    source_markdown = (
        "# Adeu.ai Legal Playbook (Standard B2B SaaS)\n\n"
        "## 1. GOVERNING LAW & DISPUTE RESOLUTION\n"
        "*   **Preferred**: State of New York or State of Delaware.\n"
        "*   **Acceptable**: England & Wales (for EMEA).\n"
        "*   **Blocking**: California (due to labor conflicts), Texas.\n"
        "*   **Venue**: Must match the governing law.\n\n"
        "## 2. ASSIGNMENT & CHANGE OF CONTROL (CRITICAL)\n"
        "*   **Requirement**: Adeu.ai must have the right to assign the agreement "
        "in the event of a merger, acquisition, or sale of assets **without** the "
        "Vendor's consent.\n"
        "*   **Reasoning**: Investors (Series B+) require clean exit paths. Any "
        '"consent required" clause for M&A is a deal-breaker.\n\n'
        "## 3. INDEMNIFICATION\n"
        "*   **Vendor Obligations**: Vendor MUST indemnify Adeu.ai for:\n"
        "    1.  Intellectual Property (IP) infringement.\n"
        "    2.  Data breaches caused by Vendor.\n"
        "    3.  Gross negligence or willful misconduct.\n"
        "*   **Our Obligations**: We typically only indemnify for our own content "
        "or unauthorized use of the service.\n\n"
        "## 4. LIMITATION OF LIABILITY\n"
        "*   **Cap Structure**: Mutual liability caps.\n"
        "*   **Minimum Cap**: 12 months of fees paid or payable.\n"
        "*   **Super Caps (Unlimited)**: Liability must be **uncapped** for:\n"
        "    *   Indemnification obligations.\n"
        "    *   Breach of confidentiality.\n"
        "    *   Fraud/Willful Misconduct.\n\n"
        "## 5. PAYMENT TERMS\n"
        "*   **Standard**: Net 45 days.\n"
        "*   **Fallback**: Net 30 days.\n"
        "*   **Reject**: Anything less than Net 30.\n"
    )

    # The target text provided by the LLM (stripped of formatting and list markers)
    target_text = (
        "Cap Structure: Mutual liability caps.\n"
        "Minimum Cap: 12 months of fees paid or payable.\n"
        "Super Caps (Unlimited): Liability must be uncapped for:\n"
        "Indemnification obligations.\n"
        "Breach of confidentiality.\n"
        "Fraud/Willful Misconduct."
    )

    # The edit we expect to apply
    edit = DocumentEdit(
        target_text=target_text,
        new_text="",  # Deletion
        comment="Policy requires mutual caps.",
    )

    # Apply edits
    result = apply_edits_to_markdown(source_markdown, [edit])

    # Check if the start of the target was marked as deleted
    assert "{--*   **Cap Structure**: Mutual liability caps." in result

    # Check if the end of the target was marked
    assert "Fraud/Willful Misconduct.--}" in result
