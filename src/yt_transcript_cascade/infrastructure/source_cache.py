"""Dual-layer filesystem cache (Source Cache + ASR Cache) with Atomic Write, TTL, LRU, and fingerprint validation."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..domain.models import SourceType, TranscriptResult

SCHEMA_VERSION = "1.2.2"


def build_asr_config_hash(config: dict[str, Any]) -> str:
    """Compute canonical SHA-256 hash of sorted ASR configuration parameters."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_asr_cache_key(
    *,
    video_id: str,
    language: str,
    engine: str,
    engine_version: str,
    model: str,
    model_fingerprint: str,
    audio_fingerprint: str,
    config_hash: str,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    """Compute canonical SHA-256 key for ASR Cache (PRD v1.2.2 §5.2)."""
    key_tuple = {
        "video_id": video_id,
        "language": language.lower(),
        "engine": engine,
        "engine_version": engine_version,
        "model": model,
        "model_fingerprint": model_fingerprint,
        "audio_fingerprint": audio_fingerprint,
        "config_hash": config_hash,
        "schema_version": schema_version,
    }
    canonical = json.dumps(key_tuple, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically using a PID/thread-unique temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _touch_lru(path: Path) -> None:
    try:
        now = time.time()
        os.utime(path, (now, now))
    except OSError:
        pass


class SourceCache:
    """Dual-layer cache managing L1 Source Cache and L2/L3 ASR Cache with TTL + LRU max_bytes governance."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        asr_cache_dir: Path | None = None,
        ttl_days: int = 30,
        max_bytes: int = 500 * 1024 * 1024,
    ) -> None:
        base_root = Path.home() / ".cache" / "yt-transcript-cascade"
        self.cache_dir = cache_dir if cache_dir is not None else (base_root / "source_cache")
        if asr_cache_dir is not None:
            self.asr_cache_dir = asr_cache_dir
        elif cache_dir is not None:
            self.asr_cache_dir = cache_dir.parent / "asr_cache"
        else:
            self.asr_cache_dir = base_root / "asr_cache"

        self.ttl_days = ttl_days
        self.max_bytes = max_bytes
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.asr_cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # L1 Source Cache: (video_id, language, source_type, schema_version)
    # ------------------------------------------------------------------
    def _source_entry_path(self, video_id: str, language: str, source_type: SourceType | None = None) -> Path:
        safe_lang = "".join(c if c.isalnum() or c in "-_" else "_" for c in language.lower())
        if source_type is not None:
            return self.cache_dir / f"{video_id}__{safe_lang}__{source_type.value}__v{SCHEMA_VERSION}.json"
        return self.cache_dir / f"{video_id}__{safe_lang}__v{SCHEMA_VERSION}.json"

    def get(self, video_id: str, languages: list[str]) -> TranscriptResult | None:
        """Return first non-expired valid cached L1 TranscriptResult matching languages priority."""
        now = time.time()
        ttl_sec = self.ttl_days * 86400

        for lang in languages:
            # Prefer MANUAL_SUBTITLE over AUTO_SUBTITLE, plus legacy M1 path compatibility
            candidates = [
                self._source_entry_path(video_id, lang, SourceType.MANUAL_SUBTITLE),
                self._source_entry_path(video_id, lang, SourceType.AUTO_SUBTITLE),
                self._source_entry_path(video_id, lang, None),
            ]
            for path in candidates:
                if not path.exists():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    created_ts = float(payload.get("cached_at", 0.0))
                    if payload.get("schema_version") != SCHEMA_VERSION or (now - created_ts) > ttl_sec:
                        path.unlink(missing_ok=True)
                        continue
                    result = TranscriptResult.model_validate(payload["result"])
                    _touch_lru(path)
                    return result.model_copy(update={"cached": True})
                except Exception:
                    path.unlink(missing_ok=True)
        return None

    def put(self, result: TranscriptResult) -> Path:
        """Persist L1 Source Cache entry atomically and enforce LRU max_bytes."""
        lang_key = result.source_language or "default"
        path = self._source_entry_path(result.video_id, lang_key, result.source_type)
        now = time.time()
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "cached_at": now,
            "last_accessed_at": now,
            "video_id": result.video_id,
            "source_type": result.source_type.value,
            "language": lang_key,
            "result": result.model_dump(mode="json"),
        }
        _atomic_write_json(path, envelope)
        self._evict_lru_if_needed(keep_path=path)
        return path

    # ------------------------------------------------------------------
    # L2/L3 ASR Cache: full 9-tuple fingerprint key (PRD v1.2.2 §5.2)
    # ------------------------------------------------------------------
    def _asr_entry_path(self, video_id: str, cache_key: str) -> Path:
        return self.asr_cache_dir / f"{video_id}__{cache_key[:24]}__v{SCHEMA_VERSION}.json"

    def get_asr(
        self,
        *,
        video_id: str,
        language: str,
        engine: str,
        engine_version: str,
        model: str,
        model_fingerprint: str,
        audio_fingerprint: str,
        config_hash: str,
    ) -> TranscriptResult | None:
        """Return valid ASR Cache entry iff all 9 key fields match and entry is non-expired."""
        expected_fields = {
            "video_id": video_id,
            "language": language.lower(),
            "engine": engine,
            "engine_version": engine_version,
            "model": model,
            "model_fingerprint": model_fingerprint,
            "audio_fingerprint": audio_fingerprint,
            "config_hash": config_hash,
            "schema_version": SCHEMA_VERSION,
        }
        cache_key = build_asr_cache_key(**expected_fields)
        path = self._asr_entry_path(video_id, cache_key)
        if not path.exists():
            return None

        now = time.time()
        ttl_sec = self.ttl_days * 86400
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_ts = float(payload.get("cached_at", 0.0))
            stored_fields = payload.get("key_fields")
            if (
                payload.get("schema_version") != SCHEMA_VERSION
                or (now - created_ts) > ttl_sec
                or stored_fields != expected_fields
            ):
                path.unlink(missing_ok=True)
                return None
            result = TranscriptResult.model_validate(payload["result"])
            _touch_lru(path)
            return result.model_copy(update={"cached": True})
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def put_asr(
        self,
        result: TranscriptResult,
        *,
        language: str,
        engine: str,
        engine_version: str,
        model: str,
        model_fingerprint: str,
        audio_fingerprint: str,
        config_hash: str,
    ) -> Path:
        """Persist ASR Cache entry atomically with full 9-tuple fingerprint and enforce LRU max_bytes."""
        key_fields = {
            "video_id": result.video_id,
            "language": language.lower(),
            "engine": engine,
            "engine_version": engine_version,
            "model": model,
            "model_fingerprint": model_fingerprint,
            "audio_fingerprint": audio_fingerprint,
            "config_hash": config_hash,
            "schema_version": SCHEMA_VERSION,
        }
        cache_key = build_asr_cache_key(**key_fields)
        path = self._asr_entry_path(result.video_id, cache_key)
        now = time.time()
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "cached_at": now,
            "last_accessed_at": now,
            "video_id": result.video_id,
            "cache_key": cache_key,
            "key_fields": key_fields,
            "result": result.model_dump(mode="json"),
        }
        _atomic_write_json(path, envelope)
        self._evict_lru_if_needed(keep_path=path)
        return path

    # ------------------------------------------------------------------
    # LRU Capacity Governance, Stats & Mutual-Exclusion Cleanup
    # ------------------------------------------------------------------
    def _all_cache_files(self) -> list[Path]:
        files: list[Path] = []
        for directory in (self.cache_dir, self.asr_cache_dir):
            if directory.exists():
                files.extend(p for p in directory.glob("*.json") if p.is_file())
        return files

    def _evict_lru_if_needed(self, *, keep_path: Path | None = None) -> int:
        if self.max_bytes <= 0:
            return 0
        entries: list[tuple[float, int, Path]] = []
        total_bytes = 0
        for p in self._all_cache_files():
            try:
                st = p.stat()
                entries.append((st.st_mtime, st.st_size, p))
                total_bytes += st.st_size
            except OSError:
                continue

        if total_bytes <= self.max_bytes:
            return 0

        entries.sort(key=lambda item: item[0])  # Oldest accessed first
        evicted = 0
        for _, size, p in entries:
            if total_bytes <= self.max_bytes:
                break
            if keep_path is not None and p == keep_path:
                continue
            try:
                p.unlink(missing_ok=True)
                total_bytes -= size
                evicted += 1
            except OSError:
                continue
        return evicted

    def _dir_stats(self, directory: Path) -> dict[str, int]:
        entries = 0
        total_bytes = 0
        if directory.exists():
            for p in directory.glob("*.json"):
                if p.is_file():
                    try:
                        total_bytes += p.stat().st_size
                        entries += 1
                    except OSError:
                        continue
        return {"entries": entries, "bytes": total_bytes}

    def stats(self) -> dict[str, Any]:
        return {
            "source_cache": self._dir_stats(self.cache_dir),
            "asr_cache": self._dir_stats(self.asr_cache_dir),
            "ttl_days": self.ttl_days,
            "max_bytes": self.max_bytes,
        }

    def clear_by_video_id(self, video_id: str) -> int:
        removed = 0
        for directory in (self.cache_dir, self.asr_cache_dir):
            if not directory.exists():
                continue
            for p in directory.glob(f"{video_id}__*.json"):
                p.unlink(missing_ok=True)
                removed += 1
        return removed

    def clear_expired(self) -> int:
        now = time.time()
        ttl_sec = self.ttl_days * 86400
        removed = 0
        for p in self._all_cache_files():
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
        for p in self._all_cache_files():
            p.unlink(missing_ok=True)
            removed += 1
        return removed
