"""L1 yt-dlp single-language subtitle adapter (strict argv list, no shell=True, anti-429)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain.models import CascadeError, ErrorCode, SourceType, TranscriptSegment

_VTT_TS_RE = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class SubtitleFetchOutput:
    video_id: str
    title: str | None
    channel: str | None
    duration_sec: float | None
    source_type: SourceType
    source_language: str
    segments: list[TranscriptSegment]
    download_sec: float
    warnings: list[str]


def _ts_to_sec(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt_segments(vtt_text: str) -> list[TranscriptSegment]:
    """Parse WebVTT text into deduplicated monotonic TranscriptSegments."""
    segments: list[TranscriptSegment] = []
    lines = vtt_text.splitlines()
    i = 0
    while i < len(lines):
        m = _VTT_TS_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _ts_to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _ts_to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _TAG_RE.sub("", lines[i]).strip()
            if cleaned and (not buf or buf[-1] != cleaned):
                buf.append(cleaned)
            i += 1
        text = " ".join(buf).strip()
        if not text:
            continue
        if segments and segments[-1].text == text:
            segments[-1] = segments[-1].model_copy(update={"end": max(segments[-1].end, end)})
        else:
            segments.append(TranscriptSegment(start=round(start, 3), end=round(max(end, start), 3), text=text))
    return segments


def _classify_yt_dlp_error(stderr: str, *, during_subtitle_dl: bool = False) -> CascadeError:
    low = (stderr or "").lower()
    if "429" in low or "too many requests" in low:
        return CascadeError(ErrorCode.RATE_LIMITED, "YouTube subtitle endpoint returned HTTP 429 Too Many Requests.")
    if any(k in low for k in ("private video", "sign in to confirm", "members-only", "join this channel")):
        return CascadeError(ErrorCode.AUTH_REQUIRED, "Video requires login, membership, or age verification.")
    if any(k in low for k in ("video unavailable", "has been removed", "not available in your country", "account associated")):
        return CascadeError(ErrorCode.VIDEO_UNAVAILABLE, "Video is unavailable, deleted, or region-blocked.")
    if "403" in low or "forbidden" in low:
        if during_subtitle_dl:
            return CascadeError(
                ErrorCode.SUBTITLE_403,
                "Subtitle endpoint returned HTTP 403 Forbidden (try updating yt-dlp via `yt-dlp -U`).",
                warnings=["Consider running `yt-dlp -U` to update to the latest extractor."],
            )
        return CascadeError(ErrorCode.AUTH_REQUIRED, "YouTube returned HTTP 403 Forbidden when accessing video metadata.")
    if any(k in low for k in ("timed out", "connection reset", "temporary failure", "getaddrinfo", "urlopen error", "network")):
        return CascadeError(ErrorCode.NETWORK_ERROR, "Transient network error while contacting YouTube.")
    return CascadeError(ErrorCode.UNKNOWN_ERROR, f"yt-dlp failed: {(stderr or '').strip()[:300]}")


class YtDlpAdapter:
    """L1 subtitle adapter using yt-dlp via safe argv subprocess calls."""

    def __init__(self, yt_dlp_bin: str = "yt-dlp") -> None:
        self.yt_dlp_bin = yt_dlp_bin

    def get_version(self) -> str | None:
        exe = shutil.which(self.yt_dlp_bin)
        if not exe:
            return None
        try:
            res = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return res.stdout.strip() if res.returncode == 0 else None
        except Exception:
            return None

    def _run_cmd(self, args: list[str], deadline: float) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CascadeError(ErrorCode.TIMEOUT, "Request deadline exceeded before launching yt-dlp.")
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise CascadeError(ErrorCode.TIMEOUT, "yt-dlp subprocess exceeded request deadline.") from exc
        except FileNotFoundError as exc:
            raise CascadeError(ErrorCode.UNKNOWN_ERROR, f"yt-dlp executable '{self.yt_dlp_bin}' not found on PATH.") from exc

    @staticmethod
    def _pick_language(available: dict[str, object], priority: list[str]) -> str | None:
        if not available:
            return None
        avail_keys = list(available.keys())
        lower_map = {k.lower(): k for k in avail_keys}
        for want in priority:
            w = want.lower()
            if w in lower_map:
                return lower_map[w]
            # Prefix match (e.g. 'en' matches 'en-US')
            for k in avail_keys:
                if k.lower().startswith(w + "-"):
                    return k
        return None

    def fetch_subtitle(
        self,
        video_id: str,
        languages: list[str],
        *,
        deadline: float,
    ) -> SubtitleFetchOutput:
        """Probe available tracks via --dump-json, then fetch ONLY the single best language."""
        t0 = time.monotonic()
        url = f"https://www.youtube.com/watch?v={video_id}"

        # Step 1: Metadata probe (confirms public video accessibility & exact subtitle keys)
        probe_res = self._run_cmd(
            [self.yt_dlp_bin, "--no-update", "--skip-download", "--dump-json", url],
            deadline=deadline,
        )
        if probe_res.returncode != 0 or not probe_res.stdout.strip():
            raise _classify_yt_dlp_error(probe_res.stderr, during_subtitle_dl=False)

        try:
            info = json.loads(probe_res.stdout)
        except json.JSONDecodeError as exc:
            raise CascadeError(ErrorCode.UNKNOWN_ERROR, "Failed to parse yt-dlp metadata JSON.") from exc

        manual_subs = info.get("subtitles") or {}
        auto_subs = info.get("automatic_captions") or {}

        chosen_lang = self._pick_language(manual_subs, languages)
        source_type = SourceType.MANUAL_SUBTITLE
        sub_flag = "--write-subs"

        if not chosen_lang:
            chosen_lang = self._pick_language(auto_subs, languages)
            source_type = SourceType.AUTO_SUBTITLE
            sub_flag = "--write-auto-subs"

        if not chosen_lang:
            raise CascadeError(
                ErrorCode.NO_SUBTITLE,
                f"No manual or auto subtitles found for video '{video_id}' matching languages {languages}.",
            )

        # Step 2: Single-language subtitle download into isolated temp dir named by video_id
        with tempfile.TemporaryDirectory(prefix=f"yt_sub_{video_id}_") as tmp_dir:
            out_tpl = str(Path(tmp_dir) / f"{video_id}.%(ext)s")
            dl_res = self._run_cmd(
                [
                    self.yt_dlp_bin,
                    "--no-update",
                    "--skip-download",
                    sub_flag,
                    "--sub-langs",
                    chosen_lang,
                    "--sub-format",
                    "vtt",
                    "-o",
                    out_tpl,
                    url,
                ],
                deadline=deadline,
            )
            if dl_res.returncode != 0:
                raise _classify_yt_dlp_error(dl_res.stderr, during_subtitle_dl=True)

            vtt_files = list(Path(tmp_dir).glob("*.vtt"))
            if not vtt_files:
                raise CascadeError(
                    ErrorCode.NO_SUBTITLE,
                    f"yt-dlp completed but produced no .vtt file for '{video_id}' ({chosen_lang}).",
                )
            vtt_text = vtt_files[0].read_text(encoding="utf-8", errors="replace")

        segments = parse_vtt_segments(vtt_text)
        if not segments:
            raise CascadeError(ErrorCode.NO_SUBTITLE, f"Subtitle file for '{video_id}' contained 0 text segments.")

        return SubtitleFetchOutput(
            video_id=video_id,
            title=info.get("title"),
            channel=info.get("channel") or info.get("uploader"),
            duration_sec=float(info["duration"]) if info.get("duration") is not None else None,
            source_type=source_type,
            source_language=chosen_lang,
            segments=segments,
            download_sec=round(time.monotonic() - t0, 3),
            warnings=[],
        )
