"""
On-disk projection cache for Adeu CLI reads.

Skips DOCX parsing and Virtual Text projection on repeated CLI reads (`adeu extract`)
of unchanged files.
"""

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from adeu import __version__
from adeu.outline import OutlineNode


def get_default_cache_dir() -> Path:
    env_dir = os.environ.get("ADEU_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "adeu" / "Cache"
        return Path.home() / ".cache" / "adeu"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "adeu"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            return Path(xdg) / "adeu"
        return Path.home() / ".cache" / "adeu"


def is_cache_disabled() -> bool:
    v1 = os.environ.get("ADEU_NO_CACHE", "").strip().lower()
    v2 = os.environ.get("ADEU_DISABLE_DISK_CACHE", "").strip().lower()
    return v1 in ("1", "true", "yes") or v2 in ("1", "true", "yes")


def serialize_outline_nodes(nodes: Optional[List[Any]]) -> Optional[List[Dict[str, Any]]]:
    if nodes is None:
        return None
    res = []
    for node in nodes:
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            res.append(dataclasses.asdict(node))
        elif isinstance(node, dict):
            res.append(node)
    return res


def deserialize_outline_nodes(nodes_data: Optional[List[Any]]) -> Optional[List[OutlineNode]]:
    if nodes_data is None:
        return None
    res = []
    for d in nodes_data:
        if isinstance(d, dict):
            res.append(
                OutlineNode(
                    level=d["level"],
                    text=d["text"],
                    page=d["page"],
                    style=d["style"],
                    has_table=d["has_table"],
                    footnote_ids=d.get("footnote_ids", []),
                    end_page=d.get("end_page"),
                )
            )
        elif isinstance(d, OutlineNode):
            res.append(d)
    return res


class DiskProjectionCache:
    def __init__(self, cache_dir: Optional[Union[Path, str]] = None):
        self._custom_cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._hits = 0
        self._misses = 0

    @property
    def cache_dir(self) -> Path:
        if self._custom_cache_dir is not None:
            return self._custom_cache_dir
        return get_default_cache_dir()

    @cache_dir.setter
    def cache_dir(self, value: Optional[Union[Path, str]]) -> None:
        self._custom_cache_dir = Path(value) if value is not None else None

    @property
    def stats(self) -> Dict[str, int]:
        return {"hits": self._hits, "misses": self._misses}

    def clear_stats(self) -> None:
        self._hits = 0
        self._misses = 0

    def get_stat_triple(self, file_path: Union[Path, str]) -> Optional[Tuple[str, int, int]]:
        try:
            p = Path(file_path).resolve()
            st = p.stat()
            return (str(p), st.st_mtime_ns, st.st_size)
        except Exception:
            return None

    def _get_cache_file_path(self, abspath: str) -> Path:
        key_hash = hashlib.sha256(abspath.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key_hash}.json"

    def get(self, file_path: Union[Path, str], clean_view: bool = False) -> Optional[Dict[str, Any]]:
        """
        Retrieves cached projection dict for `file_path` and `clean_view`.
        Returns None if cache is disabled, entry is missing, invalid, or corrupt.
        """
        if is_cache_disabled():
            return None

        stat_triple = self.get_stat_triple(file_path)
        if stat_triple is None:
            return None

        abspath, mtime_ns, size = stat_triple
        cache_file = self._get_cache_file_path(abspath)

        try:
            if not cache_file.is_file():
                self._misses += 1
                return None

            text_data = cache_file.read_text(encoding="utf-8")
            data = json.loads(text_data)

            key = data.get("key")
            if not isinstance(key, dict):
                self._misses += 1
                return None

            if (
                key.get("abspath") != abspath
                or key.get("mtime_ns") != mtime_ns
                or key.get("size") != size
                or key.get("version") != __version__
            ):
                self._misses += 1
                return None

            views = data.get("views")
            if not isinstance(views, dict):
                self._misses += 1
                return None

            view_key = "clean" if clean_view else "raw"
            view_data = views.get(view_key)
            if not isinstance(view_data, dict):
                self._misses += 1
                return None

            self._hits += 1
            res = dict(view_data)
            if "outline_nodes" in res and res["outline_nodes"] is not None:
                res["outline_nodes"] = deserialize_outline_nodes(res["outline_nodes"])
            return res

        except Exception:
            self._misses += 1
            return None

    def put(self, file_path: Union[Path, str], clean_view: bool, view_data: Dict[str, Any]) -> None:
        """
        Stores view projection dict into cache for `file_path` and `clean_view`.
        Fails silently on any I/O / permission error.
        """
        if is_cache_disabled():
            return

        stat_triple = self.get_stat_triple(file_path)
        if stat_triple is None:
            return

        abspath, mtime_ns, size = stat_triple

        try:
            c_dir = self.cache_dir
            c_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._get_cache_file_path(abspath)

            key_dict = {
                "abspath": abspath,
                "mtime_ns": mtime_ns,
                "size": size,
                "version": __version__,
            }

            data: Dict[str, Any] = {"key": key_dict, "views": {}}

            if cache_file.is_file():
                try:
                    existing_raw = cache_file.read_text(encoding="utf-8")
                    existing_data = json.loads(existing_raw)
                    if (
                        isinstance(existing_data, dict)
                        and existing_data.get("key") == key_dict
                        and isinstance(existing_data.get("views"), dict)
                    ):
                        data = existing_data
                except Exception:
                    pass

            view_key = "clean" if clean_view else "raw"
            serializable_view = dict(view_data)
            if "outline_nodes" in serializable_view and serializable_view["outline_nodes"] is not None:
                serializable_view["outline_nodes"] = serialize_outline_nodes(serializable_view["outline_nodes"])

            data.setdefault("views", {})[view_key] = serializable_view

            # Serialize and write
            content = json.dumps(data, ensure_ascii=False)
            tmp_file = cache_file.with_suffix(".tmp")
            tmp_file.write_text(content, encoding="utf-8")
            tmp_file.replace(cache_file)
        except Exception:
            pass


disk_projection_cache = DiskProjectionCache()
