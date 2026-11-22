"""Filesystem Source Cache with Atomic Write, TTL eviction, and corruption recovery."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..domain.models import TranscriptResult

SCHEMA_VERSION = "1.2.2"


class SourceCache:
    """M1 Source Cache storing L1 subtitle TranscriptResult keyed by (video_id, lang, schema)."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        ttl_days: int = 30,
        max_bytes: int = 500 * 1024 * 1024,
    ) -> None:
        self.cache_dir = (
            cache_dir
            if cache_dir is not None
            else Path.home() / ".cache" / "yt-transcript-cascade" / "source_cache"
        )
        self.ttl_days = ttl_days
        self.max_bytes = max_bytes
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _entry_path(self, video_id: str, language: str) -> Path:
        safe_lang = "".join(c if c.isalnum() or c in "-_" else "_" for c in language.lower())
        return self.cache_dir / f"{video_id}__{safe_lang}__v{SCHEMA_VERSION}.json"

    def get(self, video_id: str, languages: list[str]) -> TranscriptResult | None:
        """Return first non-expired valid cached TranscriptResult matching languages priority."""
        now = time.time()
        ttl_sec = self.ttl_days * 86400

        for lang in languages:
            path = self._entry_path(video_id, lang)
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                created_ts = float(payload.get("cached_at", 0.0))
                if payload.get("schema_version") != SCHEMA_VERSION or (now - created_ts) > ttl_sec:
                    path.unlink(missing_ok=True)
                    continue
                result = TranscriptResult.model_validate(payload["result"])
                return result.model_copy(update={"cached": True})
            except Exception:
                # Corrupted cache file -> evict cleanly
                path.unlink(missing_ok=True)
        return None

    def put(self, result: TranscriptResult) -> Path:
        """Persist TranscriptResult atomically using temp file + os.replace."""
        lang_key = result.source_language or "default"
        path = self._entry_path(result.video_id, lang_key)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        envelope = {
            "schema_version": SCHEMA_VERSION,
            "cached_at": time.time(),
            "video_id": result.video_id,
            "result": result.model_dump(mode="json"),
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    def stats(self) -> dict[str, Any]:
        entries = 0
        total_bytes = 0
        if self.cache_dir.exists():
            for p in self.cache_dir.glob("*.json"):
                if p.is_file():
                    entries += 1
                    total_bytes += p.stat().st_size
        return {
            "source_cache": {"entries": entries, "bytes": total_bytes},
            "asr_cache": {"entries": 0, "bytes": 0},
            "ttl_days": self.ttl_days,
            "max_bytes": self.max_bytes,
        }

    def clear_by_video_id(self, video_id: str) -> int:
        removed = 0
        for p in self.cache_dir.glob(f"{video_id}__*.json"):
            p.unlink(missing_ok=True)
            removed += 1
        return removed

    def clear_expired(self) -> int:
        now = time.time()
        ttl_sec = self.ttl_days * 86400
        removed = 0
        for p in self.cache_dir.glob("*.json"):
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                created_ts = float(payload.get("cached_at", 0.0))
                if payload.get("schema_version") != SCHEMA_VERSION or (now - created_ts) > ttl_sec:
                    p.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                p.unlink(missing_ok=True)
                removed += 1
        return removed

    def clear_all(self) -> int:
        removed = 0
        for p in self.cache_dir.glob("*.json"):
            p.unlink(missing_ok=True)
            removed += 1
        return removed
