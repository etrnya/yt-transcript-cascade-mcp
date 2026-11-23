"""Fixed-audio local ASR RTF benchmark tool (M3 Release Gate helper)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yt_transcript_cascade.application.quality_analyzer import analyze_transcript_quality
from yt_transcript_cascade.infrastructure.asr_engines import (
    FasterWhisperEngine,
    OpenAIWhisperEngine,
    compute_model_fingerprint,
    get_engine_version,
)
from yt_transcript_cascade.infrastructure.hardware_detector import (
    HardwareDetector,
    build_oom_fallback_chain,
)


def _probe_duration_sec(audio_path: Path) -> float | None:
    import subprocess

    try:
        res = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            return round(float(res.stdout.strip()), 3)
    except Exception:
        pass
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Benchmark local ASR RTF on fixed audio files.")
    parser.add_argument("audio_files", nargs="+", type=Path, help="Local audio file(s) to benchmark")
    parser.add_argument("--engine", choices=["faster-whisper", "openai-whisper"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output raw JSON report")
    args = parser.parse_args()

    hw = HardwareDetector().detect()
    strategies = build_oom_fallback_chain(hw, preferred_engine=args.engine, preferred_model=args.model)
    if not strategies:
        print("No available local ASR strategy detected.", file=sys.stderr)
        return 1

    strat = strategies[0]
    engine = FasterWhisperEngine() if strat.engine == "faster-whisper" else OpenAIWhisperEngine()
    engine_ver = get_engine_version(strat.engine)
    model_fp = compute_model_fingerprint(strat, engine_version=engine_ver)

    load_sec = engine.load_model(strat)
    records = []
    try:
        for audio_path in args.audio_files:
            if not audio_path.exists():
                print(f"Skipping missing file: {audio_path}", file=sys.stderr)
                continue
            t0 = time.monotonic()
            duration_sec = _probe_duration_sec(audio_path)
            audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            segments, detected_lang, asr_sec = engine.transcribe(audio_path, language=args.language)
            total_sec = round(time.monotonic() - t0, 3)
            rtf = round(asr_sec / duration_sec, 4) if duration_sec and duration_sec > 0 else None
            quality = analyze_transcript_quality(
                segments,
                duration_sec=duration_sec,
                expected_languages=[args.language] if args.language else None,
                detected_language=detected_lang,
            )
            records.append(
                {
                    "file": audio_path.name,
                    "audio_sha256": audio_sha[:16],
                    "duration_sec": duration_sec,
                    "engine": strat.engine,
                    "engine_version": engine_ver,
                    "device": strat.device,
                    "compute_type": strat.compute_type,
                    "model": strat.model_name,
                    "model_fingerprint": model_fp,
                    "model_load_sec": load_sec,
                    "asr_sec": asr_sec,
                    "total_elapsed_sec": total_sec,
                    "rtf": rtf,
                    "segments_count": len(segments),
                    "detected_language": detected_lang,
                    "quality_status": quality.status,
                    "duration_coverage_ratio": quality.duration_coverage_ratio,
                }
            )
    finally:
        engine.release()

    report = {"hardware": hw.model_dump(mode="json"), "benchmarks": records}
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Hardware: {hw.device_name} ({hw.backend}, VRAM={hw.vram_available_mb}/{hw.vram_total_mb} MiB)")
        print(f"Strategy: {strat.engine} v{engine_ver} | {strat.model_name} | {strat.device}:{strat.compute_type} (load={load_sec}s)\n")
        print("| File | Duration (s) | ASR (s) | RTF | Segments | Quality |")
        print("| :--- | ---: | ---: | ---: | ---: | :---: |")
        for r in records:
            print(
                f"| `{r['file']}` | {r['duration_sec']} | {r['asr_sec']} | **{r['rtf']}** | {r['segments_count']} | `{r['quality_status']}` |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
