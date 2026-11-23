"""M3 reliability, dual-layer ASR cache, cross-process lock, and security tests."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yt_transcript_cascade.application.orchestrator import CascadeOrchestrator
from yt_transcript_cascade.domain.models import (
    CascadeError,
    CascadeTier,
    ClearCacheInput,
    ErrorCode,
    ExecutionMode,
    GetTranscriptInput,
    HardwareProfile,
    SourceType,
    TranscriptQualityReport,
    TranscriptResult,
    TranscriptSegment,
)
from yt_transcript_cascade.infrastructure.file_lock import CrossProcessFileLock
from yt_transcript_cascade.infrastructure.hardware_detector import AsrStrategy
from yt_transcript_cascade.infrastructure.source_cache import SourceCache, build_asr_config_hash
from yt_transcript_cascade.infrastructure.yt_dlp_adapter import AudioDownloadOutput, SubtitleFetchOutput


def _sample_result(
    video_id: str = "dQw4w9WgXcQ",
    *,
    source_type: SourceType = SourceType.LOCAL_ASR,
    tier_used: CascadeTier = CascadeTier.L2_LOCAL_ASR,
    source_language: str = "zh",
    text: str = "測試逐字稿內容",
) -> TranscriptResult:
    return TranscriptResult(
        video_id=video_id,
        title="Safe Title",
        channel="Ch",
        duration_sec=10.0,
        source_type=source_type,
        source_language=source_language,
        is_generated=(source_type != SourceType.MANUAL_SUBTITLE),
        tier_used=tier_used,
        engine_used="faster-whisper" if tier_used == CascadeTier.L2_LOCAL_ASR else "yt-dlp",
        model_used="small" if tier_used == CascadeTier.L2_LOCAL_ASR else None,
        model_fingerprint="faster-whisper:small:int8:deadbeef" if tier_used == CascadeTier.L2_LOCAL_ASR else None,
        device_used="cpu:int8" if tier_used == CascadeTier.L2_LOCAL_ASR else "network",
        download_sec=0.2,
        model_load_sec=0.3,
        asr_sec=0.5,
        total_elapsed_sec=1.0,
        rtf=0.05 if tier_used == CascadeTier.L2_LOCAL_ASR else None,
        segments=[TranscriptSegment(start=0.0, end=10.0, text=text)],
        quality_report=TranscriptQualityReport(
            structural_ok=True,
            empty_segment_count=0,
            repetition_anomaly_count=0,
            duration_coverage_ratio=1.0,
            language_consistent=True,
            status="OK",
        ),
        cached=False,
        warnings=[],
    )


class _CountingAsrEngine:
    def __init__(self, name: str = "faster-whisper") -> None:
        self.name = name
        self.load_calls = 0
        self.transcribe_calls = 0

    def check_available(self, strategy: AsrStrategy) -> bool:
        return strategy.engine == self.name

    def load_model(self, strategy: AsrStrategy) -> float:
        self.load_calls += 1
        return 0.1

    def transcribe(self, audio_path: Path, *, language: str | None = None, deadline: float | None = None):
        self.transcribe_calls += 1
        return [TranscriptSegment(start=0.0, end=10.0, text="ASR 產出內容")], "zh", 0.4

    def release(self) -> None:
        pass


class TestM3DualLayerCache(unittest.TestCase):
    def test_asr_cache_9_tuple_fingerprint_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SourceCache(cache_dir=Path(td) / "source_cache")
            cfg_hash = build_asr_config_hash({"beam_size": 5, "vad_filter": True})
            base_kwargs = {
                "video_id": "dQw4w9WgXcQ",
                "language": "zh-Hant",
                "engine": "faster-whisper",
                "engine_version": "1.2.1",
                "model": "turbo",
                "model_fingerprint": "fp-model-v1",
                "audio_fingerprint": "a" * 64,
                "config_hash": cfg_hash,
            }
            cache.put_asr(_sample_result(), **{k: v for k, v in base_kwargs.items() if k != "video_id"})

            # Exact match hits
            hit = cache.get_asr(**base_kwargs)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertTrue(hit.cached)

            # Mutating any fingerprint or version invalidates (R3 P1-1)
            for field, mutated in [
                ("model_fingerprint", "fp-model-v2"),
                ("audio_fingerprint", "b" * 64),
                ("engine_version", "1.3.0"),
                ("config_hash", build_asr_config_hash({"beam_size": 1, "vad_filter": False})),
                ("model", "small"),
            ]:
                bad = dict(base_kwargs)
                bad[field] = mutated
                self.assertIsNone(cache.get_asr(**bad), f"Expected miss when {field} changed")

    def test_corrupted_asr_cache_auto_evicts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SourceCache(cache_dir=Path(td) / "source_cache")
            cfg_hash = build_asr_config_hash({"beam_size": 5})
            path = cache.put_asr(
                _sample_result(),
                language="zh",
                engine="faster-whisper",
                engine_version="1.2.1",
                model="small",
                model_fingerprint="fp-1",
                audio_fingerprint="c" * 64,
                config_hash=cfg_hash,
            )
            path.write_text("{corrupted-json", encoding="utf-8")
            res = cache.get_asr(
                video_id="dQw4w9WgXcQ",
                language="zh",
                engine="faster-whisper",
                engine_version="1.2.1",
                model="small",
                model_fingerprint="fp-1",
                audio_fingerprint="c" * 64,
                config_hash=cfg_hash,
            )
            self.assertIsNone(res)
            self.assertFalse(path.exists())

    def test_lru_max_bytes_eviction_and_dual_layer_stats_clear(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SourceCache(cache_dir=Path(td) / "source_cache", max_bytes=10 * 1024 * 1024)
            p1 = cache.put(_sample_result("aaaaaaaaaaa", source_type=SourceType.MANUAL_SUBTITLE, tier_used=CascadeTier.L1_SUBTITLE))
            p2 = cache.put_asr(
                _sample_result("bbbbbbbbbbb"),
                language="zh",
                engine="faster-whisper",
                engine_version="1.2.1",
                model="small",
                model_fingerprint="fp",
                audio_fingerprint="d" * 64,
                config_hash="e" * 64,
            )
            os.utime(p1, (1000, 1000))
            os.utime(p2, (2000, 2000))
            # Set max_bytes to fit 2 entries but not 3 -> inserting p3 must evict oldest (p1)
            cache.max_bytes = p1.stat().st_size + p2.stat().st_size + 100
            p3 = cache.put(_sample_result("ccccccccccc", source_type=SourceType.AUTO_SUBTITLE, tier_used=CascadeTier.L1_SUBTITLE))
            self.assertFalse(p1.exists())
            self.assertTrue(p2.exists())
            self.assertTrue(p3.exists())

            stats = cache.stats()
            self.assertEqual(stats["source_cache"]["entries"], 1)
            self.assertEqual(stats["asr_cache"]["entries"], 1)
            removed = cache.clear_all()
            self.assertEqual(removed, 2)
            self.assertEqual(cache.stats()["source_cache"]["entries"], 0)
            self.assertEqual(cache.stats()["asr_cache"]["entries"], 0)

    def test_source_cache_prefers_manual_over_auto_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SourceCache(cache_dir=Path(td) / "source_cache")
            cache.put(_sample_result("dQw4w9WgXcQ", source_type=SourceType.AUTO_SUBTITLE, tier_used=CascadeTier.L1_SUBTITLE, text="auto"))
            cache.put(_sample_result("dQw4w9WgXcQ", source_type=SourceType.MANUAL_SUBTITLE, tier_used=CascadeTier.L1_SUBTITLE, text="manual"))
            hit = cache.get("dQw4w9WgXcQ", ["zh"])
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.source_type, SourceType.MANUAL_SUBTITLE)
            self.assertEqual(hit.segments[0].text, "manual")

    def test_orchestrator_asr_cache_hit_skips_model_load_and_transcribe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = MagicMock()

            def fake_download(video_id: str, out_dir: Path, *, deadline: float) -> AudioDownloadOutput:
                p = out_dir / f"{video_id}.m4a"
                p.write_bytes(b"same-audio-bytes")
                return AudioDownloadOutput(
                    video_id=video_id,
                    title="../../evil; rm -rf /",
                    channel="Ch",
                    duration_sec=10.0,
                    audio_path=p,
                    audio_fingerprint="f" * 64,
                    download_sec=0.1,
                )

            mock_yt.download_audio.side_effect = fake_download
            mock_hw = MagicMock()
            mock_hw.detect.return_value = HardwareProfile(
                platform="windows",
                accelerator="cpu",
                device_name="CPU",
                backend="cpu_int8",
                backend_verified=True,
                supported_engines=["faster-whisper"],
            )
            engine = _CountingAsrEngine("faster-whisper")
            cache = SourceCache(cache_dir=root / "source_cache")
            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=cache,
                hardware_detector=mock_hw,
                asr_engines={"faster-whisper": engine},
                allowed_save_root=root,
            )

            req = GetTranscriptInput(url="https://youtu.be/mIg76INLBMU", execution_mode=ExecutionMode.LOCAL_ASR_ONLY)
            r1 = orch.get_transcript(req)
            self.assertTrue(r1["ok"])
            self.assertFalse(r1["result"]["cached"])
            self.assertEqual(engine.load_calls, 1)
            self.assertEqual(engine.transcribe_calls, 1)

            # Verify stored in asr_cache (not source_cache) and filename uses video_id only
            stats = orch.get_cache_stats()
            self.assertEqual(stats["source_cache"]["entries"], 0)
            self.assertEqual(stats["asr_cache"]["entries"], 1)
            for f in cache.asr_cache_dir.glob("*.json"):
                self.assertTrue(f.name.startswith("mIg76INLBMU__"))
                self.assertNotIn("evil", f.name)

            # Second call hits ASR cache -> 0 additional model load or transcribe calls
            r2 = orch.get_transcript(req)
            self.assertTrue(r2["ok"])
            self.assertTrue(r2["result"]["cached"])
            self.assertEqual(engine.load_calls, 1)
            self.assertEqual(engine.transcribe_calls, 1)

            # clear_transcript_cache by video_id removes ASR cache entry too
            clr = orch.clear_cache(ClearCacheInput(video_id="mIg76INLBMU"))
            self.assertTrue(clr["ok"])
            self.assertEqual(clr["removed_entries"], 1)
            self.assertEqual(orch.get_cache_stats()["asr_cache"]["entries"], 0)


class TestM3ConcurrencyAndCrossProcessLock(unittest.TestCase):
    def test_in_process_single_flight_deduplicates_concurrent_threads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = MagicMock()
            fetch_calls = 0

            def slow_fetch(video_id: str, languages: list[str], *, deadline: float) -> SubtitleFetchOutput:
                nonlocal fetch_calls
                fetch_calls += 1
                time.sleep(0.2)
                return SubtitleFetchOutput(
                    video_id=video_id,
                    title="Concurrent Video",
                    channel="Ch",
                    duration_sec=10.0,
                    source_type=SourceType.MANUAL_SUBTITLE,
                    source_language="zh-Hant",
                    segments=[TranscriptSegment(start=0.0, end=10.0, text="單飛測試")],
                    download_sec=0.2,
                    warnings=[],
                )

            mock_yt.fetch_subtitle.side_effect = slow_fetch
            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=SourceCache(cache_dir=root / "source_cache"),
                allowed_save_root=root,
            )

            results: list[dict] = []
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    results.append(orch.get_transcript(GetTranscriptInput(url="https://youtu.be/dQw4w9WgXcQ")))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 4)
            self.assertTrue(all(r["ok"] for r in results))
            self.assertEqual(fetch_calls, 1)
            self.assertEqual(sum(1 for r in results if r["result"]["cached"]), 3)

    def test_cross_process_file_lock_mutual_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "dQw4w9WgXcQ.lock"
            src_dir = str(Path(__file__).resolve().parents[1] / "src")
            holder_code = "\n".join(
                [
                    "import sys, time",
                    f"sys.path.insert(0, {src_dir!r})",
                    "from pathlib import Path",
                    "from yt_transcript_cascade.infrastructure.file_lock import CrossProcessFileLock",
                    f"with CrossProcessFileLock(Path({str(lock_path)!r}), timeout_sec=5.0):",
                    "    print('LOCKED', flush=True)",
                    "    time.sleep(0.7)",
                ]
            )
            with subprocess.Popen(
                [sys.executable, "-c", holder_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            ) as proc:
                assert proc.stdout is not None
                line = proc.stdout.readline().strip()
                self.assertEqual(line, "LOCKED")

                # Short timeout must fail with TIMEOUT while child process holds the lock
                with self.assertRaises(CascadeError) as ctx:
                    with CrossProcessFileLock(lock_path, timeout_sec=0.15):
                        pass
                self.assertEqual(ctx.exception.code, ErrorCode.TIMEOUT)

                # Longer timeout must wait and succeed once child releases
                with CrossProcessFileLock(lock_path, timeout_sec=3.0):
                    acquired = True
                self.assertTrue(acquired)
                proc.wait(timeout=5)


class TestM3SecurityInvariants(unittest.TestCase):
    def test_no_shell_true_anywhere_in_src(self) -> None:
        src_root = Path(__file__).resolve().parents[1] / "src" / "yt_transcript_cascade"
        violations: list[str] = []
        for py_file in src_root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            violations.append(f"{py_file.name}:{node.lineno}")
        self.assertEqual(violations, [], f"Forbidden shell=True found at: {violations}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
