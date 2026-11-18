#!/usr/bin/env python3
"""Headless batch subtitler — same engine as the web UI, no server needed.

    python cli.py ~/videos --model small --bilingual --recursive
    python cli.py movie.mp4 --lang fr --format both
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from app import core


def process(video: str, args) -> str:
    t0 = time.time()
    print(f"\n→ {os.path.basename(video)}", flush=True)

    def progress(frac):
        print(f"\r  transcribing {frac * 100:5.1f}%", end="", flush=True)

    cues, info = core.transcribe(
        video,
        size=args.model,
        language=None if args.lang in ("auto", None) else args.lang,
        threads=args.threads,
        models_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
        beam_size=args.beam,
        on_progress=progress,
        initial_prompt=args.prompt or None,
        hotwords=", ".join(t for t, _ in core.parse_glossary(args.glossary_text)[:60]) or None,
    )
    print(f"\r  transcribed {len(cues)} segments (lang={info.language}, "
          f"{time.time() - t0:.0f}s)", flush=True)
    src = (info.language or args.lang or "en").lower()

    final: list[core.Cue] = []
    if args.translate and src != "ar":
        print(f"  translating {src} → ar …", flush=True)
        ar = core.translate(
            [c.text for c in cues], src, "ar",
            glossary=core.parse_glossary(args.glossary_text),
            keep_foreign=args.keep_foreign,
        )
        for c, t in zip(cues, ar):
            final.extend(core.split_pair(c.start, c.end, t, c.text, max_chars=args.max_chars))
    else:
        for c in cues:
            final.extend(core.split_pair(c.start, c.end, c.text, "", max_chars=args.max_chars))
    final = core.merge_tiny(final)

    suffix = ".ar" if args.naming == "lang_suffix" else ""
    stem = os.path.splitext(video)[0] + suffix
    if not args.beside:
        stem = os.path.join(args.out, suffix.strip(".") or "subs",
                            os.path.splitext(os.path.basename(video))[0])

    written = []
    if args.format in ("srt", "both"):
        p = stem + ".srt"
        core.write_srt(final, p, bom=args.bom, rtl_marks=args.rtl,
                       bilingual=args.bilingual, max_chars=args.max_chars)
        written.append(p)
    if args.format in ("ass", "both"):
        p = stem + ".ass"
        core.write_ass(final, p, bilingual=args.bilingual, max_chars=args.max_chars)
        written.append(p)

    for p in written:
        print(f"  ✓ {p}  ({os.path.getsize(p)} bytes)", flush=True)
    print(f"  done in {time.time() - t0:.0f}s", flush=True)
    return written[0] if written else ""


def main():
    ap = argparse.ArgumentParser(description="Generate Arabic subtitles for videos.")
    ap.add_argument("target", help="video file or folder")
    ap.add_argument("--model", default="small", choices=["tiny", "base", "small"])
    ap.add_argument("--lang", default="auto", help="source language code, or 'auto'")
    ap.add_argument("--no-translate", action="store_true", help="transcribe only")
    ap.add_argument("--bilingual", action="store_true", help="original line + Arabic")
    ap.add_argument("--format", default="srt", choices=["srt", "ass", "both"])
    ap.add_argument("--naming", default="lang_suffix", choices=["lang_suffix", "plain"])
    ap.add_argument("--beside", action="store_true", default=True,
                    help="write next to the video (default)")
    ap.add_argument("--out", default="output", help="output folder when not --beside")
    ap.add_argument("--no-bom", dest="bom", action="store_false")
    ap.add_argument("--rtl", action="store_true")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--max-chars", type=int, default=42)
    ap.add_argument("--recursive", action="store_true", default=True)
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--glossary", default=None,
                    help="file with one term per line ('term' or 'term = translation')")
    ap.add_argument("--prompt", default="", help="hint about what the lesson says (names, vocabulary)")
    ap.add_argument("--no-keep-foreign", dest="keep_foreign", action="store_false",
                    help="do not protect French words automatically")
    args = ap.parse_args()
    args.glossary_text = ""
    if args.glossary:
        with open(args.glossary, encoding="utf-8") as f:
            args.glossary_text = f.read()

    target = os.path.abspath(os.path.expanduser(args.target))
    if os.path.isfile(target):
        files = [target]
    elif os.path.isdir(target):
        files = [i["path"] for i in core.scan_videos(target, args.recursive) if i["has_audio"]]
    else:
        sys.exit(f"not found: {target}")

    if not files:
        sys.exit("no video files found")

    total = sum(core.probe_duration(f) for f in files)
    print(f"{len(files)} file(s), {total / 3600:.1f} h of audio")

    for f in files:
        skip = False
        if args.skip_existing:
            stem = os.path.splitext(f)[0] + (".ar" if args.naming == "lang_suffix" else "")
            skip = os.path.exists(stem + ".srt")
        if skip:
            print(f"\n= skip (has subtitle): {os.path.basename(f)}")
            continue
        try:
            process(f, args)
        except KeyboardInterrupt:
            sys.exit("\ninterrupted")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  ✗ failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
