"""Comprehensive offline unit & contract tests for M1 (aligned with PRD v1.2.2)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yt_transcript_cascade.application.cascade_policy import CascadePolicy
from yt_transcript_cascade.application.formatter import render_transcript
from yt_transcript_cascade.application.orchestrator import CascadeOrchestrator
from yt_transcript_cascade.application.quality_analyzer import analyze_transcript_quality
from yt_transcript_cascade.domain.models import (
    CascadeAction,
    CascadeError,
    ClearCacheInput,
    ErrorCode,
    ExecutionMode,
    GetTranscriptInput,
    OutputFormat,
    SourceType,
    TranscriptSegment,
)
from yt_transcript_cascade.domain.url_parser import parse_youtube_video_id, validate_save_path
from yt_transcript_cascade.infrastructure.source_cache import SourceCache
from yt_transcript_cascade.infrastructure.yt_dlp_adapter import SubtitleFetchOutput, parse_vtt_segments


class TestUrlAndPathSecurity(unittest.TestCase):
    def test_valid_youtube_urls(self) -> None:
        self.assertEqual(parse_youtube_video_id("https://www.youtube.com/watch?v=mIg76INLBMU"), "mIg76INLBMU")
        self.assertEqual(parse_youtube_video_id("https://youtu.be/mIg76INLBMU?t=42"), "mIg76INLBMU")
        self.assertEqual(parse_youtube_video_id("https://www.youtube.com/shorts/mIg76INLBMU"), "mIg76INLBMU")
        self.assertEqual(parse_youtube_video_id("mIg76INLBMU"), "mIg76INLBMU")

    def test_reject_non_youtube_and_injection(self) -> None:
        for bad in [
            "",
            "https://evil.com/watch?v=mIg76INLBMU",
            "file:///etc/passwd",
            "https://www.youtube.com/watch?v=abc;rm+-rf+/",
            "javascript:alert(1)",
        ]:
            with self.assertRaises(CascadeError) as ctx:
                parse_youtube_video_id(bad)
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_path_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ok = validate_save_path("notes/demo.md", root)
            self.assertTrue(str(ok).startswith(str(root.resolve())))

            for bad in ["../escape.md", "../../Windows/win.ini", ""]:
                with self.assertRaises(CascadeError) as ctx:
                    validate_save_path(bad, root)
                self.assertEqual(ctx.exception.code, ErrorCode.PATH_VIOLATION)


class TestCascadePolicyAndTruthTables(unittest.TestCase):
    def test_all_14_error_codes_covered(self) -> None:
        self.assertEqual(len(ErrorCode), 14)
        self.assertEqual(set(CascadePolicy.all_error_codes()), set(ErrorCode))

    def test_retry_exhaustion_uses_exhausted_action(self) -> None:
        d0 = CascadePolicy.decide(ErrorCode.NETWORK_ERROR, attempt=0)
        self.assertTrue(d0.retryable)
        self.assertEqual(d0.cascade_action, CascadeAction.RETRY)

        d_exhausted = CascadePolicy.decide(ErrorCode.NETWORK_ERROR, attempt=2)
        self.assertFalse(d_exhausted.retryable)
        self.assertEqual(d_exhausted.cascade_action, CascadeAction.TERMINATE)

    def test_cloud_authorization_decoupled(self) -> None:
        # COLAB_ONLY without allow_cloud_processing -> INVALID_INPUT
        req = GetTranscriptInput(
            url="https://youtu.be/mIg76INLBMU",
            execution_mode=ExecutionMode.COLAB_ONLY,
            enable_colab_fallback=True,
            allow_cloud_processing=False,
        )
        with self.assertRaises(CascadeError) as ctx:
            CascadePolicy.validate_request(req)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

        # ASR_BACKEND_UNAVAILABLE dynamic user_action_required
        d_no_opt = CascadePolicy.decide(ErrorCode.ASR_BACKEND_UNAVAILABLE, enable_colab_fallback=False)
        self.assertTrue(d_no_opt.user_action_required)
        self.assertEqual(d_no_opt.cascade_action, CascadeAction.TERMINATE)

        d_opt = CascadePolicy.decide(ErrorCode.ASR_BACKEND_UNAVAILABLE, enable_colab_fallback=True)
        self.assertFalse(d_opt.user_action_required)
        self.assertEqual(d_opt.cascade_action, CascadeAction.FALLBACK_L3)

    def test_clear_cache_8_row_truth_table(self) -> None:
        vid = "mIg76INLBMU"
        valid_cases = [
            (ClearCacheInput(video_id=vid, expired_only=False, clear_all=False), "video_id"),
            (ClearCacheInput(video_id=None, expired_only=True, clear_all=False), "expired_only"),
            (ClearCacheInput(video_id=None, expired_only=False, clear_all=True), "clear_all"),
        ]
        for inp, expected in valid_cases:
            self.assertEqual(CascadePolicy.validate_clear_cache_input(inp), expected)

        invalid_cases = [
            ClearCacheInput(video_id=None, expired_only=False, clear_all=False),
            ClearCacheInput(video_id=vid, expired_only=True, clear_all=False),
            ClearCacheInput(video_id=vid, expired_only=False, clear_all=True),
            ClearCacheInput(video_id=None, expired_only=True, clear_all=True),
            ClearCacheInput(video_id=vid, expired_only=True, clear_all=True),
        ]
        for inp in invalid_cases:
            with self.assertRaises(CascadeError) as ctx:
                CascadePolicy.validate_clear_cache_input(inp)
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)


class TestVttQualityFormatterAndCache(unittest.TestCase):
    def test_parse_vtt_and_quality_ok(self) -> None:
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:05.000\n<c>Hello</c> world\n\n"
            "00:00:05.000 --> 00:00:10.000\nSecond line\n"
        )
        segs = parse_vtt_segments(vtt)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0].text, "Hello world")

        q = analyze_transcript_quality(
            segs, duration_sec=10.0, expected_languages=["en"], detected_language="en-US"
        )
        self.assertEqual(q.status, "OK")
        self.assertTrue(q.structural_ok)
        self.assertEqual(q.duration_coverage_ratio, 1.0)
        self.assertTrue(q.language_consistent)

    def test_quality_flags_repetition_and_low_coverage(self) -> None:
        segs = [
            TranscriptSegment(start=0.0, end=1.0, text="same"),
            TranscriptSegment(start=1.0, end=2.0, text="same"),
            TranscriptSegment(start=2.0, end=3.0, text="same"),
        ]
        q = analyze_transcript_quality(segs, duration_sec=100.0)
        self.assertEqual(q.status, "WARNING")
        self.assertGreater(q.repetition_anomaly_count, 0)

    def test_orchestrator_l1_success_cache_hit_and_md_export(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = SourceCache(cache_dir=root / "cache")
            mock_yt = MagicMock()
            mock_yt.fetch_subtitle.return_value = SubtitleFetchOutput(
                video_id="mIg76INLBMU",
                title="Demo Video",
                channel="Demo Channel",
                duration_sec=12.0,
                source_type=SourceType.MANUAL_SUBTITLE,
                source_language="zh-Hant",
                segments=[
                    TranscriptSegment(start=0.0, end=6.0, text="第一段測試"),
                    TranscriptSegment(start=6.0, end=12.0, text="第二段測試"),
                ],
                download_sec=0.42,
                warnings=[],
            )
            orch = CascadeOrchestrator(yt_dlp_adapter=mock_yt, source_cache=cache, allowed_save_root=root)

            # First call -> L1 + saves .md
            res1 = orch.get_transcript(
                GetTranscriptInput(url="https://youtu.be/mIg76INLBMU", save_to_path="out/demo.md")
            )
            self.assertTrue(res1["ok"])
            self.assertFalse(res1["result"]["cached"])
            self.assertEqual(res1["result"]["source_type"], "MANUAL_SUBTITLE")
            md_path = Path(res1["result"]["server_saved_path"])
            self.assertTrue(md_path.exists())
            md_text = md_path.read_text(encoding="utf-8")
            self.assertIn("source_type: MANUAL_SUBTITLE", md_text)
            self.assertIn("- [00:00] 第一段測試", md_text)

            # Second call -> cache hit (yt-dlp called only once)
            res2 = orch.get_transcript(GetTranscriptInput(url="https://youtu.be/mIg76INLBMU"))
            self.assertTrue(res2["ok"])
            self.assertTrue(res2["result"]["cached"])
            self.assertEqual(mock_yt.fetch_subtitle.call_count, 1)

            # Corrupt cache recovery
            for p in (root / "cache").glob("*.json"):
                p.write_text("{corrupt json", encoding="utf-8")
            self.assertIsNone(cache.get("mIg76INLBMU", ["zh-Hant"]))

    def test_orchestrator_handles_429_and_auth_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mock_yt = MagicMock()
            mock_yt.fetch_subtitle.side_effect = CascadeError(ErrorCode.RATE_LIMITED, "HTTP 429")
            orch = CascadeOrchestrator(
                yt_dlp_adapter=mock_yt,
                source_cache=SourceCache(cache_dir=root / "cache"),
                allowed_save_root=root,
            )
            res = orch.get_transcript(GetTranscriptInput(url="https://youtu.be/mIg76INLBMU"))
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"]["code"], "RATE_LIMITED")

            mock_yt.fetch_subtitle.side_effect = CascadeError(ErrorCode.AUTH_REQUIRED, "Private video")
            res_priv = orch.get_transcript(GetTranscriptInput(url="https://youtu.be/mIg76INLBMU"))
            self.assertFalse(res_priv["ok"])
            self.assertEqual(res_priv["error"]["code"], "AUTH_REQUIRED")
            self.assertTrue(res_priv["error"]["user_action_required"])


class TestStdioPurity(unittest.TestCase):
    def test_import_server_produces_zero_stdout(self) -> None:
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path.insert(0, {src_path!r}); import yt_transcript_cascade.server as s; assert len(s.mcp._tool_manager.list_tools()) == 4",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout, "", msg=f"Unexpected stdout pollution: {proc.stdout!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
