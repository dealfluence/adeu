"""
Server-layer projection cache for read_docx.

Port of the Node MCP server's document cache (docs/Performance.md §5.1):

- Key: (absolute path, mtime_ns, size) — stat-checked on EVERY call, so any
  rewrite of the file (including by this server's own tools) changes the key
  and invalidation is automatic. No TTLs, no explicit flushes.
- Value: PROJECTIONS ONLY — projected text, pagination, outline nodes.
  Never the parsed lxml tree: trees are mutable, huge, and their lifetime is
  the single compute call.
- LRU-bounded to MAX_ENTRIES documents (measured worst case on the 45 MB
  stress document is ~75 MB per fully-populated entry, dominated by the
  page-content strings).
- Per-entry compute lock: concurrent requests for the same cold document run
  the projection once; the loser blocks in its worker thread (the event loop
  stays free) and then reads the memo.

Every artifact is deterministic from the file bytes (projection goldens
prove run-to-run byte-stability), so a cache hit is byte-identical to a
fresh computation by construction — and tests/test_doc_cache.py asserts
exactly that against cache-less passes.
"""

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from docx import Document

from adeu.ingest import _extract_text_from_doc
from adeu.outline import OutlineNode, extract_outline
from adeu.pagination import PaginationResult, paginate, split_structural_appendix
from adeu.utils.docx import strip_bom_from_docx_bytes

MAX_ENTRIES = 3

ProgressFn = Optional[Callable[[int, str], None]]


@dataclass(slots=True)
class _View:
    """Cached artifacts for one (document version, clean_view) pair."""

    base_text: Optional[str] = None  # _extract_text_from_doc(include_appendix=False)
    text_with_appendix: Optional[str] = None  # include_appendix=True (mode='appendix')
    pagination: Optional[PaginationResult] = None  # paginate(split(base_text).body, "")
    outline_nodes: Optional[List[OutlineNode]] = None


@dataclass(slots=True)
class _Entry:
    key: Tuple[str, int, int]
    raw: _View = field(default_factory=_View)
    clean: _View = field(default_factory=_View)
    lock: threading.Lock = field(default_factory=threading.Lock)
    clean_fill_scheduled: bool = False

    def view(self, clean_view: bool) -> _View:
        return self.clean if clean_view else self.raw


def _progress(cb: ProgressFn, pct: int, msg: str) -> None:
    if cb is not None:
        cb(pct, msg)


class DocProjectionCache:
    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._entries: "OrderedDict[Tuple[str, int, int], _Entry]" = OrderedDict()
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()
        self._last_activity = 0.0

    # ------------------------------------------------------------------ keys

    @staticmethod
    def stat_key(file_path: str) -> Tuple[str, int, int]:
        """Version key for the CURRENT bytes of file_path. Raises OSError /
        FileNotFoundError like os.stat."""
        p = Path(file_path).resolve()
        st = os.stat(p)
        return (str(p), st.st_mtime_ns, st.st_size)

    def entry(self, key: Tuple[str, int, int]) -> _Entry:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(key=key)
                self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return entry

    # -------------------------------------------------------------- activity

    def mark_activity(self) -> None:
        """Called at the start of every read so the background clean-view
        fill can wait for a quiet server (Node lesson: an immediate
        multi-second fill stalls the page-2 request that typically follows)."""
        self._last_activity = time.monotonic()

    def quiet_for(self, seconds: float) -> bool:
        return (time.monotonic() - self._last_activity) >= seconds

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # -------------------------------------------------------------- computes
    #
    # All computes run in worker threads (callers use asyncio.to_thread) and
    # take the per-entry lock, so one cold projection serves every waiter.
    #
    # One cold compute fills text + pagination + outline nodes together: the
    # load dominates the marginal outline cost, the paragraph-offset map the
    # outline needs references live lxml elements from ITS OWN parse (so
    # outline can never be derived from a cached string later), and a warm
    # cache must serve every read mode — page turns, search, AND outline —
    # in milliseconds. Only the appendix variant stays lazy (rare + several
    # seconds of defined-terms scanning).

    @staticmethod
    def _load_document(path: str, cb: ProgressFn):
        _progress(cb, 5, "reading file")
        data = Path(path).read_bytes()
        _progress(cb, 10, "checking encoding")
        sanitized = strip_bom_from_docx_bytes(data)
        _progress(cb, 20, "parsing document XML")
        doc = Document(BytesIO(sanitized))
        _progress(cb, 35, "projecting text")
        return doc

    def _fill_view(self, entry: _Entry, clean_view: bool, cb: ProgressFn) -> _View:
        """Cold compute. Caller must hold entry.lock."""
        view = entry.view(clean_view)
        doc = self._load_document(entry.key[0], cb)
        text, offsets = _extract_text_from_doc(
            doc,
            clean_view=clean_view,
            include_appendix=False,
            return_paragraph_offsets=True,
        )
        _progress(cb, 80, "paginating")
        body, _ = split_structural_appendix(text)
        pagination = paginate(body, structural_appendix="")
        _progress(cb, 88, "building outline")
        nodes = extract_outline(
            doc,
            body,
            pagination.body_pages,
            pagination.body_page_offsets,
            paragraph_offsets=offsets,
        )
        # Assign only after everything computed, so a mid-compute exception
        # leaves the view fully cold instead of half-filled.
        view.base_text = text
        view.pagination = pagination
        view.outline_nodes = nodes
        return view

    def _view_ready(self, entry: _Entry, clean_view: bool, cb: ProgressFn) -> _View:
        view = entry.view(clean_view)
        with entry.lock:
            if view.base_text is None or view.pagination is None or view.outline_nodes is None:
                view = self._fill_view(entry, clean_view, cb)
            return view

    def get_base_text(self, entry: _Entry, clean_view: bool, cb: ProgressFn = None) -> str:
        view = self._view_ready(entry, clean_view, cb)
        assert view.base_text is not None
        return view.base_text

    def get_pagination(self, entry: _Entry, clean_view: bool, cb: ProgressFn = None) -> Tuple[str, PaginationResult]:
        view = self._view_ready(entry, clean_view, cb)
        assert view.base_text is not None and view.pagination is not None
        return view.base_text, view.pagination

    def get_outline(
        self, entry: _Entry, clean_view: bool, cb: ProgressFn = None
    ) -> Tuple[str, PaginationResult, List[OutlineNode]]:
        view = self._view_ready(entry, clean_view, cb)
        assert view.base_text is not None and view.pagination is not None and view.outline_nodes is not None
        return view.base_text, view.pagination, view.outline_nodes

    def get_text_with_appendix(self, entry: _Entry, clean_view: bool, cb: ProgressFn = None) -> str:
        view = entry.view(clean_view)
        with entry.lock:
            if view.text_with_appendix is None:
                doc = self._load_document(entry.key[0], cb)
                view.text_with_appendix = _extract_text_from_doc(doc, clean_view=clean_view, include_appendix=True)
                _progress(cb, 90, "paginating")
            return view.text_with_appendix

    def is_cold(self, entry: _Entry, clean_view: bool) -> bool:
        return entry.view(clean_view).base_text is None


# Module singleton used by the MCP tools.
doc_cache = DocProjectionCache()
