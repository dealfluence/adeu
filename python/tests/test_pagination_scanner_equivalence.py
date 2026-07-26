"""
Pins the regex-scan implementation of pagination's
_split_on_safe_paragraph_breaks against a VERBATIM copy of the historical
char-by-char walk (the docs/Performance.md §3.6 discipline: performance
rewrites ship with the old algorithm as an executable specification).

The old walk called str.startswith at nearly every position — 37 million
calls on a 4.6 MB projection — and was replaced by a single compiled-regex
pass. Any semantic drift between the two must fail here, not in production
pagination.
"""

import random

from adeu.pagination import _CRITIC_TOKENS, _split_on_safe_paragraph_breaks


def _split_charwise_verbatim(text):
    """VERBATIM copy of the historical implementation (do not modernize)."""
    counters = {close: 0 for close in _CRITIC_TOKENS.values()}
    blocks = []
    block_start = 0
    i = 0
    n = len(text)

    while i < n:
        # Try to match an open token first.
        matched_open = False
        for open_tok, close_tok in _CRITIC_TOKENS.items():
            if text.startswith(open_tok, i):
                counters[close_tok] += 1
                i += len(open_tok)
                matched_open = True
                break
        if matched_open:
            continue

        # Try to match a close token.
        matched_close = False
        for close_tok in _CRITIC_TOKENS.values():
            if text.startswith(close_tok, i):
                if counters[close_tok] > 0:
                    counters[close_tok] -= 1
                # If unbalanced, still consume so we don't loop forever.
                i += len(close_tok)
                matched_close = True
                break
        if matched_close:
            continue

        # Check for a paragraph break.
        if text[i] == "\n" and i + 1 < n and text[i + 1] == "\n":
            if all(c == 0 for c in counters.values()):
                block_text = text[block_start:i]
                if block_text:
                    blocks.append((block_text, block_start))

                j = i
                while j < n and text[j] == "\n":
                    j += 1
                i = j
                block_start = i
                continue

        i += 1

    if block_start < n:
        block_text = text[block_start:n]
        if block_text:
            blocks.append((block_text, block_start))

    return blocks


CRAFTED_CASES = [
    "",
    "\n",
    "\n\n",
    "\n\n\n\n",
    "plain text with no breaks",
    "para one\n\npara two\n\npara three",
    "para one\n\n\n\npara two",  # collapsed multi-newline boundary
    "leading\n\n",
    "\n\nleading break",
    "{++ inserted\n\nstill inside ++}\n\nafter",
    "{-- deleted --}\n\n{++ added ++}",
    "{== hl ==}{>> comment with\n\nbreak <<}\n\nnext",
    "{++ nest {-- inner --} outer\n\nsplit? ++}\n\nyes",
    "unbalanced close ++} then\n\nbreak",
    "unbalanced open {++ never closed\n\nno boundary here",
    "{++{++ double open ++}\n\nstill depth 1 ++}\n\nfree",
    "token fragments: { + +} - -- = ==\n\n<< <}",
    "{==}",  # open token consumes, lone brace remains
    "+++}",  # close matches at offset 1
    "{{++ off-by-one open\n\n++}",
    "ends with open {>>",
    "ends mid newline\n",
    "tab\tand spaces  \n\n  next block  ",
    "{-- a --}{-- b --}{-- c --}\n\nd",
    "a\n\n \n\nb",  # whitespace-only block between boundaries
    "\n\n{++x++}\n\n",
]


def test_crafted_cases_identical():
    for case in CRAFTED_CASES:
        assert _split_on_safe_paragraph_breaks(case) == _split_charwise_verbatim(case), repr(case)


def test_randomized_streams_identical():
    rng = random.Random(20260724)
    atoms = (
        list("abc \n")
        + ["\n\n", "\n\n\n"]
        + list(_CRITIC_TOKENS.keys())
        + list(_CRITIC_TOKENS.values())
        + ["{", "}", "+", "-", "=", ">", "<", "{+", "+}", "-}", "{>", "<}"]
    )
    for _ in range(3000):
        s = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 60)))
        assert _split_on_safe_paragraph_breaks(s) == _split_charwise_verbatim(s), repr(s)


def test_projection_shaped_document_identical():
    # A synthetic projection-shaped document: headings, tables (single-newline
    # rows), CriticMarkup spanning paragraph breaks, footnote sections.
    rng = random.Random(42)
    parts = []
    for i in range(400):
        kind = rng.randrange(6)
        if kind == 0:
            parts.append(f"# Heading {i}")
        elif kind == 1:
            parts.append(f"cell a{i} | cell b{i}\n--- | ---\nrow2 | row2b")
        elif kind == 2:
            parts.append(f"{{++ inserted paragraph {i} with\n\ninternal break ++}}{{>>[Chg:{i} insert] Author<<}}")
        elif kind == 3:
            parts.append(f"{{-- deleted {i} --}}{{>>[Chg:{i} delete] Author<<}}")
        elif kind == 4:
            parts.append(f"[^fn-{i}]: footnote definition {i}")
        else:
            parts.append(f"Ordinary paragraph {i} " + "lorem ipsum " * rng.randrange(1, 8))
    doc = "\n\n".join(parts)
    assert _split_on_safe_paragraph_breaks(doc) == _split_charwise_verbatim(doc)
