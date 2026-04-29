import inspect

from adeu.mcp_components.tools.document import process_document_batch


def test_process_document_batch_docstring_mentions_attribution():
    """
    OBS-01 Update: The process_document_batch docstring must explicitly state
    that author_name is correctly used for attribution, even in Live Word.
    """
    source = inspect.getsource(process_document_batch)

    assert "spoofed" not in source.lower(), (
        "Docstring should no longer claim identities cannot be spoofed, as this was disproved."
    )
    assert "used for attribution" in source.lower(), (
        "Docstring must explicitly state that author_name is used for attribution."
    )
