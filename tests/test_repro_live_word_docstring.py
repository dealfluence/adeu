import inspect

from adeu.mcp_components.tools.document import process_document_batch


def test_process_document_batch_docstring_mentions_live_spoofing():
    """
    Bug #9: The process_document_batch docstring must explicitly warn the LLM
    that author_name cannot be spoofed in Live Word.
    """
    source = inspect.getsource(process_document_batch)

    assert "live word" in source.lower() and "spoofed" in source.lower(), (
        "Docstring must explicitly warn that live Word identities cannot be spoofed."
    )
