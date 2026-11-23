"""M4 offline tests: ColabRuntimeLifecycleManager, 3-strike STOP_FAILED, cooldown, L3 fallback & cloud_upload_notice."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
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
from yt_transcript_cascade.infrastructure.colab_adapter import ColabTranscribeOutput
from yt_transcript_cascade.infrastructure.colab_lifecycle import (
    ColabRuntimeLifecycleManager,
    validate_session_id,
)
from yt_transcript_cascade.infrastructure.source_cache import SourceCache
from yt_transcript_cascade.infrastructure.yt_dlp_adapter import AudioDownloadOutput


class TestM4ColabLifecycleManager(unittest.TestCase):
    def test_session_id_whitelist_blocks_injection(self) -> None:
        self.assertEqual(validate_session_id("ytc-dQw4w9WgXcQ-1234abcd"), "ytc-dQw4w9WgXcQ-1234abcd")
        for bad in ["", "abc; rm -rf /", "../etc/passwd", "space id", "a" * 65]:
            with self.assertRaises(CascadeError) as ctx:
                validate_session_id(bad)
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_normal_lifecycle_and_not_found_treated_as_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "colab_sessions.json"
            mgr = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:test",
                stop_fn=lambda sid: (True, None, True),  # remote already absent
            )
            mgr.register_session("sess-001", status="CREATED")
            mgr.update_status("sess-001", "ACTIVE")
            mgr.mark_cleanup_pending("sess-001")
            rec = mgr.cleanup_session("sess-001")
            self.assertIsNotNone(rec)
            assert rec is not None
            self.assertEqual(rec.status, "STOPPED")
            self.assertFalse(rec.cleanup_required)
            self.assertEqual(mgr.pending_session_count(), 0)

    def test_3_strike_stop_failed_never_deleted_and_cooldown_retry_does_not_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "colab_sessions.json"
            base_time = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
            current_time = [base_time]
            stop_calls: list[str] = []

            def always_fail(sid: str) -> tuple[bool, str | None, bool]:
                stop_calls.append(sid)
                return False, "Simulated 500 API error", False

            mgr = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:hostA",
                cooldown_sec=900,
                stop_fn=always_fail,
                now_fn=lambda: current_time[0],
            )
            mgr.register_session("sess-fail-1", status="ACTIVE")
            rec = mgr.cleanup_session("sess-fail-1")
            assert rec is not None
            self.assertEqual(rec.status, "STOP_FAILED")
            self.assertEqual(rec.retry_count, 3)
            self.assertEqual(len(stop_calls), 3)
            self.assertIn("Simulated 500", rec.last_error or "")
            # Record must NEVER be deleted and NEVER marked STOPPED
            self.assertEqual(len(mgr.list_sessions()), 1)
            self.assertEqual(mgr.pending_session_count(), 1)

            # Simulate restart BEFORE 15-min cooldown -> 0 extra attempts, retry_count stays 3
            current_time[0] = base_time + timedelta(minutes=5)
            mgr_restart_1 = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:hostA",
                cooldown_sec=900,
                stop_fn=always_fail,
                now_fn=lambda: current_time[0],
            )
            warns1 = mgr_restart_1.recover_pending_sessions()
            self.assertEqual(len(stop_calls), 3)
            self.assertEqual(len(warns1), 1)
            self.assertIn("STOP_FAILED", warns1[0])

            # Simulate restart AFTER 15-min cooldown -> at most 1 extra attempt (retry_count 3 -> 4)
            current_time[0] = base_time + timedelta(minutes=20)
            mgr_restart_2 = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:hostA",
                cooldown_sec=900,
                stop_fn=always_fail,
                now_fn=lambda: current_time[0],
            )
            warns2 = mgr_restart_2.recover_pending_sessions()
            self.assertEqual(len(stop_calls), 4)
            self.assertEqual(mgr_restart_2.list_sessions()[0].retry_count, 4)
            self.assertEqual(mgr_restart_2.list_sessions()[0].status, "STOP_FAILED")
            self.assertEqual(len(warns2), 1)

            # Another restart after another 20 mins -> MUST NOT retry again (no infinite reset!)
            current_time[0] = base_time + timedelta(minutes=45)
            mgr_restart_3 = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:hostA",
                cooldown_sec=900,
                stop_fn=always_fail,
                now_fn=lambda: current_time[0],
            )
            mgr_restart_3.recover_pending_sessions()
            self.assertEqual(len(stop_calls), 4)

    def test_owner_id_isolation_never_touches_other_owners(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "colab_sessions.json"
            stopped: list[str] = []
            mgr_a = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:ownerA",
                stop_fn=lambda sid: (stopped.append(sid) or True, None, False),
            )
            mgr_a.register_session("sess-owner-a", status="CLEANUP_PENDING")

            mgr_b = ColabRuntimeLifecycleManager(
                state_path=state_path,
                owner_id="yt-cascade-mcp:ownerB",
                stop_fn=lambda sid: (stopped.append(sid) or True, None, False),
            )
            mgr_b.recover_pending_sessions()
            self.assertEqual(stopped, [])
            self.assertEqual(mgr_b.pending_session_count(), 0)


class TestM4OrchestratorColabIntegration(unittest.TestCase):
    def _make_audio_mock(self) -> MagicMock:
        mock_yt = MagicMock()
        mock_yt.fetch_subtitle.side_effect = CascadeError(ErrorCode.NO_SUBTITLE, "No subtitle")

        def fake_dl(video_id: str, out_dir: Path, *, deadline: float) -> AudioDownloadOutput:
            p = out_dir / f"{video_id}.m4a"
            p.write_bytes(b"colab-audio-bytes")
            return AudioDownloadOutput(
                video_id=video_id,
                title="Colab Test Video",
                channel="Ch",
                duration_sec=20.0,
                audio_path=p,
                audio_fingerprint="9" * 64,
                download_sec=0.4,
            )

        mock_yt.download_audio.side_effect = fake_dl
        return mock_yt

    def test_auto_l2_unavailable_with_colab_fallback_emits_notice_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = self._make_audio_mock()
            mock_hw = MagicMock()
            mock_hw.detect.return_value = HardwareProfile(
                platform="linux",
                accelerator="cpu",
                device_name="CPU",
                backend="cpu_int8",
                backend_verified=False,
                supported_engines=[],  # No local ASR installed
            )

            stderr_buf = io.StringIO()
            notice_seen_before_upload = [False]

            mock_colab = MagicMock()
            mock_colab.is_available.return_value = True

            def fake_colab_transcribe(*args, **kwargs) -> ColabTranscribeOutput:
                notice_seen_before_upload[0] = "CLOUD_UPLOAD_NOTICE" in stderr_buf.getvalue()
                return ColabTranscribeOutput(
                    segments=[TranscriptSegment(start=0.0, end=20.0, text="Colab T4 雲端轉錄完成")],
                    detected_language="zh",
                    model_load_sec=3.0,
                    asr_sec=2.0,
                    device_used="colab:cuda:Tesla T4",
                    model_used="turbo",
                    model_fingerprint="colab-openai-whisper:turbo:fp16",
                    warnings=[],
                )

            mock_colab.transcribe.side_effect = fake_colab_transcribe

            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=SourceCache(cache_dir=root / "source_cache"),
                hardware_detector=mock_hw,
                asr_engines={},
                colab_adapter=mock_colab,
                allowed_save_root=root,
            )

            with redirect_stderr(stderr_buf):
                res = orch.get_transcript(
                    GetTranscriptInput(
                        url="https://youtu.be/dQw4w9WgXcQ",
                        execution_mode=ExecutionMode.AUTO,
                        enable_colab_fallback=True,
                    )
                )

            self.assertTrue(res["ok"])
            self.assertTrue(notice_seen_before_upload[0], "cloud_upload_notice must be emitted BEFORE upload")
            r = res["result"]
            self.assertEqual(r["tier_used"], "L3_COLAB_GPU")
            self.assertEqual(r["source_type"], "CLOUD_ASR")
            self.assertIn("CLOUD_UPLOAD_NOTICE", r["cloud_upload_notice"])
            self.assertEqual(r["rtf"], 0.1)
            self.assertEqual(orch.get_cache_stats()["asr_cache"]["entries"], 1)

    def test_colab_only_requires_allow_cloud_processing_and_works_when_granted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = self._make_audio_mock()
            mock_colab = MagicMock()
            mock_colab.is_available.return_value = True
            mock_colab.transcribe.return_value = ColabTranscribeOutput(
                segments=[TranscriptSegment(start=0.0, end=20.0, text="COLAB_ONLY 轉錄成功")],
                detected_language="zh",
                model_load_sec=2.5,
                asr_sec=1.5,
                device_used="colab:cuda:Tesla T4",
                model_used="turbo",
                model_fingerprint="colab-openai-whisper:turbo:fp16",
                warnings=[],
            )
            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=SourceCache(cache_dir=root / "source_cache"),
                colab_adapter=mock_colab,
                allowed_save_root=root,
            )

            # Denied when allow_cloud_processing=False
            denied = orch.get_transcript(
                GetTranscriptInput(
                    url="https://youtu.be/dQw4w9WgXcQ",
                    execution_mode=ExecutionMode.COLAB_ONLY,
                    allow_cloud_processing=False,
                )
            )
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["error"]["code"], "INVALID_INPUT")

            # Allowed when allow_cloud_processing=True -> skips L1/L2
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                ok = orch.get_transcript(
                    GetTranscriptInput(
                        url="https://youtu.be/dQw4w9WgXcQ",
                        execution_mode=ExecutionMode.COLAB_ONLY,
                        allow_cloud_processing=True,
                    )
                )
            self.assertTrue(ok["ok"])
            mock_yt.fetch_subtitle.assert_not_called()
            self.assertEqual(ok["result"]["tier_used"], "L3_COLAB_GPU")
            self.assertIn("CLOUD_UPLOAD_NOTICE", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
