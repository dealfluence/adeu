from adeu.mcp_components.tools.document import PROCESS_BATCH_OPERATIONS_DESC


def test_process_document_batch_docstring_mentions_attribution():
    """
    OBS-01 Update: The process_document_batch docstring must explicitly state
    that author_name is correctly used for attribution, even in Live Word.
    """
    desc = PROCESS_BATCH_OPERATIONS_DESC

    assert "spoofed" not in desc.lower(), (
        "Docstring should no longer claim identities cannot be spoofed, as this was disproved."
    )
    assert "used for attribution" in desc.lower(), (
        "Docstring must explicitly state that author_name is used for attribution."
    )
