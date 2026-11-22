"""M2 offline contract tests: HardwareDetector, OOM fallback chain, L1->L2 fallback, RTF & cleanup."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yt_transcript_cascade.application.orchestrator import CascadeOrchestrator
from yt_transcript_cascade.domain.models import (
    CascadeError,
    ErrorCode,
    ExecutionMode,
    GetTranscriptInput,
    HardwareProfile,
    TranscriptSegment,
)
from yt_transcript_cascade.infrastructure.hardware_detector import AsrStrategy, build_oom_fallback_chain
from yt_transcript_cascade.infrastructure.source_cache import SourceCache
from yt_transcript_cascade.infrastructure.yt_dlp_adapter import AudioDownloadOutput


class _FakeAsrEngine:
    def __init__(self, name: str, *, oom_models: set[str] | None = None) -> None:
        self.name = name
        self.oom_models = oom_models or set()
        self.loaded: list[AsrStrategy] = []
        self.released_count = 0
        self._current: AsrStrategy | None = None

    def check_available(self, strategy: AsrStrategy) -> bool:
        return strategy.engine == self.name

    def load_model(self, strategy: AsrStrategy) -> float:
        self.loaded.append(strategy)
        self._current = strategy
        if strategy.model_name in self.oom_models and strategy.device == "cuda":
            raise CascadeError(ErrorCode.ASR_OOM, f"Simulated OOM on {strategy.model_name}")
        return 0.5

    def transcribe(self, audio_path: Path, *, language: str | None = None, deadline: float | None = None):
        assert audio_path.exists()
        return (
            [
                TranscriptSegment(start=0.0, end=5.0, text="本機 GPU 轉錄測試第一句"),
                TranscriptSegment(start=5.0, end=10.0, text="第二句完成"),
            ],
            "zh",
            1.0,
        )

    def release(self) -> None:
        self.released_count += 1
        self._current = None


class TestM2HardwareAndFallback(unittest.TestCase):
    def test_oom_chain_rtx4060_and_cpu_only(self) -> None:
        rtx4060 = HardwareProfile(
            platform="windows",
            accelerator="nvidia",
            device_name="NVIDIA GeForce RTX 4060 Laptop GPU",
            vram_total_mb=8188,
            vram_available_mb=7568,
            backend="ctranslate2_cuda",
            backend_verified=True,
            supported_engines=["faster-whisper"],
        )
        chain = build_oom_fallback_chain(rtx4060)
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain[0], AsrStrategy("faster-whisper", "cuda", "float16", "turbo"))
        self.assertEqual(chain[1], AsrStrategy("faster-whisper", "cuda", "int8", "small"))
        self.assertEqual(chain[2], AsrStrategy("faster-whisper", "cpu", "int8", "small"))

        # CPU-only machine skips unsupported CUDA strategies and never repeats
        cpu_only = HardwareProfile(
            platform="linux",
            accelerator="cpu",
            device_name="x86_64",
            vram_total_mb=None,
            vram_available_mb=None,
            backend="cpu_int8",
            backend_verified=True,
            supported_engines=["faster-whisper"],
        )
        cpu_chain = build_oom_fallback_chain(cpu_only)
        self.assertEqual(cpu_chain, [AsrStrategy("faster-whisper", "cpu", "int8", "small")])

    def test_l1_429_triggers_l2_and_oom_downgrade_and_cleans_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = MagicMock()
            mock_yt.fetch_subtitle.side_effect = CascadeError(ErrorCode.RATE_LIMITED, "HTTP 429")

            created_audio_paths: list[Path] = []

            def fake_download(video_id: str, out_dir: Path, *, deadline: float) -> AudioDownloadOutput:
                p = out_dir / f"{video_id}.m4a"
                p.write_bytes(b"fake-audio-bytes")
                created_audio_paths.append(p)
                return AudioDownloadOutput(
                    video_id=video_id,
                    title="L2 Fallback Video",
                    channel="Test Channel",
                    duration_sec=10.0,
                    audio_path=p,
                    audio_fingerprint="a" * 64,
                    download_sec=0.8,
                )

            mock_yt.download_audio.side_effect = fake_download

            mock_hw = MagicMock()
            mock_hw.detect.return_value = HardwareProfile(
                platform="windows",
                accelerator="nvidia",
                device_name="NVIDIA GeForce RTX 4060 Laptop GPU",
                vram_total_mb=8188,
                vram_available_mb=7568,
                backend="ctranslate2_cuda",
                backend_verified=True,
                supported_engines=["faster-whisper"],
            )

            fake_fw = _FakeAsrEngine("faster-whisper", oom_models={"turbo"})
            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=SourceCache(cache_dir=root / "cache"),
                hardware_detector=mock_hw,
                asr_engines={"faster-whisper": fake_fw},
                allowed_save_root=root,
            )

            res = orch.get_transcript(
                GetTranscriptInput(url="https://youtu.be/mIg76INLBMU", save_to_path="out/l2.md")
            )
            self.assertTrue(res["ok"])
            r = res["result"]
            self.assertEqual(r["tier_used"], "L2_LOCAL_ASR")
            self.assertEqual(r["source_type"], "LOCAL_ASR")
            self.assertEqual(r["model_used"], "small")  # downgraded from turbo after OOM
            self.assertEqual(r["rtf"], 0.1)  # 1.0s / 10.0s
            self.assertEqual(r["download_sec"], 0.8)
            self.assertEqual(r["model_load_sec"], 0.5)
            self.assertEqual(r["asr_sec"], 1.0)
            self.assertEqual(r["metadata"]["audio_fingerprint"], "a" * 64)
            # Temp audio file cleaned up when keep_audio=False
            self.assertFalse(created_audio_paths[0].exists())
            # Release called on every attempt (OOM attempt + successful attempt)
            self.assertEqual(fake_fw.released_count, 2)

    def test_local_asr_only_skips_l1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = MagicMock()

            def fake_download(video_id: str, out_dir: Path, *, deadline: float) -> AudioDownloadOutput:
                p = out_dir / f"{video_id}.m4a"
                p.write_bytes(b"fake")
                return AudioDownloadOutput(
                    video_id=video_id,
                    title="Direct L2",
                    channel="Ch",
                    duration_sec=20.0,
                    audio_path=p,
                    audio_fingerprint="b" * 64,
                    download_sec=0.3,
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
            fake_fw = _FakeAsrEngine("faster-whisper")
            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=SourceCache(cache_dir=root / "cache"),
                hardware_detector=mock_hw,
                asr_engines={"faster-whisper": fake_fw},
                allowed_save_root=root,
            )

            res = orch.get_transcript(
                GetTranscriptInput(url="https://youtu.be/mIg76INLBMU", execution_mode=ExecutionMode.LOCAL_ASR_ONLY)
            )
            self.assertTrue(res["ok"])
            mock_yt.fetch_subtitle.assert_not_called()
            self.assertEqual(res["result"]["tier_used"], "L2_LOCAL_ASR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
