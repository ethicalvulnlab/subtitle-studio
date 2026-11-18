"""Core: media probing, audio handling, subtitle formatting, ASR and translation."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v", ".flv", ".ts", ".mpg", ".mpeg", ".wmv"}

_dur_cache: dict[str, float] = {}
_dur_lock = threading.Lock()


def probe_duration(path: str) -> float:
    """Duration in seconds, 0.0 when unreadable. Cached per path."""
    with _dur_lock:
        if path in _dur_cache:
            return _dur_cache[path]
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        dur = float(out) if out else 0.0
    except Exception:
        dur = 0.0
    with _dur_lock:
        _dur_cache[path] = dur
    return dur


def has_audio(path: str) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return bool(out)
    except Exception:
        return False


def scan_videos(root: str, recursive: bool = True) -> list[dict]:
    """List video files under root with metadata."""
    root = os.path.abspath(root)
    found: list[dict] = []
    if os.path.isfile(root):
        candidates = [root]
    else:
        candidates = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                        candidates.append(os.path.join(dirpath, fn))
        else:
            for fn in os.listdir(root):
                p = os.path.join(root, fn)
                if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    candidates.append(p)

    for p in sorted(candidates):
        try:
            st = os.stat(p)
        except OSError:
            continue
        low = p.lower()
        stem = os.path.splitext(low)[0]
        found.append({
            "path": p,
            "name": os.path.basename(p),
            "rel": os.path.relpath(p, root) if os.path.isdir(root) else os.path.basename(p),
            "dir": os.path.dirname(p),
            "size": st.st_size,
            "duration": round(probe_duration(p), 2),
            "has_ar": os.path.exists(stem + ".ar.srt") or os.path.exists(p + ".ar.srt"),
            "has_srt": os.path.exists(stem + ".srt"),
            "has_audio": has_audio(p),
            "mtime": st.st_mtime,
        })
    return found


# --------------------------------------------------------------------------- #
# Subtitles
# --------------------------------------------------------------------------- #

class Cancelled(Exception):
    """Raised when the user cancels a job mid-flight."""


@dataclass
class Cue:
    start: float
    end: float
    text: str
    orig: str = ""


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _wrap(text: str, max_chars: int = 42, max_lines: int = 2) -> str:
    """Greedy word wrap, balanced across lines."""
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        if cur and len(" ".join(cur + [w])) > max_chars and len(lines) < max_lines - 1:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    lines.append(" ".join(cur))
    if len(lines) > max_lines:
        head = " ".join(lines[: max_lines - 1])
        tail = " ".join(lines[max_lines - 1:])
        lines = [head, tail]
    return "\n".join(lines)


def write_srt(cues: Iterable[Cue], path: str, *, bom: bool = True,
              rtl_marks: bool = False, bilingual: bool = False,
              max_chars: int = 42) -> None:
    parts: list[str] = []
    for i, c in enumerate(cues, 1):
        body_lines = []
        if bilingual and c.orig and c.orig.strip() and c.orig.strip() != c.text.strip():
            body_lines.append(_wrap(c.orig.strip(), max_chars))
        ar = c.text.strip()
        if rtl_marks and ar:
            ar = "\u202b" + ar + "\u202c"
        body_lines.append(_wrap(ar, max_chars))
        parts.append(f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n" + "\n".join(body_lines) + "\n")
    data = "\n".join(parts)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\n") as f:
        f.write(data)


def write_vtt(cues: Iterable[Cue]) -> str:
    out = ["WEBVTT", ""]
    for c in cues:
        out.append(f"{_srt_time(c.start).replace(',', '.')} --> {_srt_time(c.end).replace(',', '.')}")
        out.append(c.text.strip())
        out.append("")
    return "\n".join(out)


def write_ass(cues: Iterable[Cue], path: str, *, font: str = "Arial",
              size: int = 44, bilingual: bool = False, max_chars: int = 42) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2.5,0.5,2,60,60,50,1
Style: Orig,{font},{max(18, size // 2)},&H00C8C8C8,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0.5,2,60,60,{50 + size},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for c in cues:
        ar = c.text.strip().replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Arabic,,0,0,0,,{ar}")
        if bilingual and c.orig.strip():
            orig = c.orig.strip().replace("\n", r"\N")
            lines.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Orig,,0,0,0,,{orig}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


_SENT_SPLIT = re.compile(r"(?<=[.!?؟])\s+")


def split_long_segment(start: float, end: float, text: str, max_chars: int = 42,
                       max_lines: int = 2) -> list[Cue]:
    """Break an over-long ASR segment into several cues, splitting time by length."""
    text = " ".join(text.split())
    limit = max_chars * max_lines
    if len(text) <= limit or end <= start:
        return [Cue(start, end, text)]

    chunks: list[str] = []
    for sent in _SENT_SPLIT.split(text):
        if len(sent) <= limit:
            chunks.append(sent)
            continue
        words, cur = sent.split(), []
        for w in words:
            if cur and len(" ".join(cur + [w])) > limit:
                chunks.append(" ".join(cur))
                cur = [w]
            else:
                cur.append(w)
        if cur:
            chunks.append(" ".join(cur))
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        return [Cue(start, end, text)]

    total_chars = sum(len(c) for c in chunks)
    span = end - start
    cues, t = [], start
    for c in chunks:
        share = span * (len(c) / total_chars)
        cues.append(Cue(t, min(end, t + share), c))
        t += share
    return cues


def merge_tiny(cues: list[Cue], min_dur: float = 0.6,
               max_merged: float = 7.0) -> list[Cue]:
    """Fold sub-minimum cues into the previous one.

    The merged block is capped: without the cap, a long run of short cues
    chains together into a single 30-45 s subtitle.
    """
    out: list[Cue] = []
    for c in cues:
        if (out
                and (c.end - c.start) < min_dur
                and (c.start - out[-1].end) < 0.4
                and (c.end - out[-1].start) <= max_merged):
            prev = out[-1]
            prev.text = (prev.text + " " + c.text).strip()
            prev.end = max(prev.end, c.end)
        else:
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
# ASR
# --------------------------------------------------------------------------- #

_MODELS: dict[str, object] = {}
_model_lock = threading.Lock()


def get_whisper(size: str, threads: int = 4, models_dir: str = "models"):
    from faster_whisper import WhisperModel

    key = f"{size}:{threads}"
    with _model_lock:
        if key not in _MODELS:
            try:
                # cached copy first: no network probe, so a DNS blip can't
                # break an otherwise fully offline run
                _MODELS[key] = WhisperModel(
                    size, device="cpu", compute_type="int8",
                    cpu_threads=threads, download_root=models_dir,
                    local_files_only=True,
                )
            except Exception:
                _MODELS[key] = WhisperModel(
                    size, device="cpu", compute_type="int8",
                    cpu_threads=threads, download_root=models_dir,
                )
        return _MODELS[key]


_SENT_END = ".!?؟…"


def cues_from_words(words, *, max_dur: float = 6.0, max_chars: int = 84) -> list[Cue]:
    """Rebuild cues from word timings: break on sentence end, length or duration.

    Whisper's own segmenting drifts into 20-second blocks once we hint it with
    vocabulary; per-word times keep every cue tight.
    """
    cues: list[Cue] = []
    cur = []
    for w in words:
        cur.append(w)
        text = "".join(x.word for x in cur).strip()
        dur = cur[-1].end - cur[0].start
        stripped = w.word.strip()
        ends = bool(stripped) and stripped[-1] in _SENT_END
        if ends or dur >= max_dur or len(text) >= max_chars:
            if text:
                cues.append(Cue(cur[0].start, cur[-1].end, " ".join(text.split())))
            cur = []
    if cur:
        text = " ".join("".join(x.word for x in cur).split())
        if text:
            cues.append(Cue(cur[0].start, cur[-1].end, text))
    return cues


def transcribe(video: str, *, size: str = "small", language: str | None = None,
               threads: int = 4, models_dir: str = "models",
               beam_size: int = 5, max_chars: int = 42, on_progress=None,
               word_timestamps: bool = True, should_stop=None, on_cue=None):
    """Return (cues, info). Cues are sentence-level — display splitting happens
    after translation so the translator always sees complete sentences."""
    model = get_whisper(size, threads, models_dir)
    segments, info = model.transcribe(
        video,
        language=language or None,
        beam_size=beam_size,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False,
        word_timestamps=word_timestamps,
    )

    words = []
    seg_cues: list[Cue] = []
    for seg in segments:
        if should_stop and should_stop():
            raise Cancelled()
        if word_timestamps and getattr(seg, "words", None):
            words.extend(seg.words)
        text = " ".join(seg.text.split())
        if text:
            seg_cues.append(Cue(seg.start, seg.end, text))
            if on_cue:
                on_cue(seg_cues[-1])
        if on_progress:
            on_progress(min(seg.end / info.duration, 0.999) if info.duration else 0.0)

    if word_timestamps and words:
        return cues_from_words(words), info
    return seg_cues, info


# --------------------------------------------------------------------------- #
# Arabic post-processing
# --------------------------------------------------------------------------- #

_DUP_PHRASE = re.compile(r"\b([\w\u0600-\u06FF]+(?:\s+[\w\u0600-\u06FF]+){0,3})\s+\1\b", re.UNICODE)


def clean_ar(text: str) -> str:
    """Remove the most common MT artifacts: doubled phrases, dangling conjunction."""
    t = " ".join(text.split())
    t = _DUP_PHRASE.sub(r"\1", t)
    t = re.sub(r"\s+([،.!؟])", r"\1", t)
    t = re.sub(r"^(و|ثم)\s+", "", t)
    t = re.sub(r"\s+(و|ثم)$", "", t)
    # stray / unbalanced ASCII quotes that MT likes to emit around names
    t = t.replace('"', "").replace("''", "")
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def _chunk_sentences(text: str, limit: int) -> list[str]:
    """Group sentences/words into chunks no longer than limit characters."""
    out: list[str] = []
    for sent in _SENT_SPLIT.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= limit:
            if out and len(out[-1]) + 1 + len(sent) <= limit:
                out[-1] = out[-1] + " " + sent
            else:
                out.append(sent)
            continue
        words, cur = sent.split(), []
        for w in words:
            if cur and len(" ".join(cur + [w])) > limit:
                out.append(" ".join(cur))
                cur = [w]
            else:
                cur.append(w)
        if cur:
            out.append(" ".join(cur))
    return out


def split_pair(start: float, end: float, arabic: str, original: str,
               max_chars: int = 42, max_lines: int = 2) -> list[Cue]:
    """Split a translated cue to TV size, keeping the original roughly aligned."""
    arabic = clean_ar(arabic)
    limit = max_chars * max_lines
    if len(arabic) <= limit or end <= start:
        return [Cue(start, end, arabic, original.strip())]

    chunks = _chunk_sentences(arabic, limit)
    if not chunks:
        return [Cue(start, end, arabic, original.strip())]

    orig_words = original.split()
    total_ar = sum(len(c) for c in chunks) or 1
    total_orig = len(orig_words)
    span = end - start

    cues, t, used_ar, used_orig = [], start, 0, 0
    for i, c in enumerate(chunks):
        share = len(c) / total_ar
        if i == len(chunks) - 1:
            o_slice = orig_words[used_orig:]
        else:
            take = max(0, round(total_orig * ((used_ar + len(c)) / total_ar)) - used_orig)
            o_slice = orig_words[used_orig:used_orig + take]
            used_orig += take
        used_ar += len(c)
        cues.append(Cue(t, min(end, t + span * share), c, " ".join(o_slice)))
        t += span * share
    return cues


# --------------------------------------------------------------------------- #
# Translation (MarianMT via transformers, CPU)
# --------------------------------------------------------------------------- #

_TRANS: dict[str, tuple] = {}
_trans_lock = threading.Lock()
MARIAN = {
    ("en", "ar"): "Helsinki-NLP/opus-mt-en-ar",
    ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
}
LANG_NAMES = {
    "en": "English", "fr": "French", "ar": "Arabic", "es": "Spanish",
    "de": "German", "it": "Italian", "pt": "Portuguese", "tr": "Turkish",
    "ru": "Russian", "nl": "Dutch", "auto": "Auto-detect",
}


def _load_marian(src: str, tgt: str):
    key = (src, tgt)
    with _trans_lock:
        if key not in _TRANS:
            from transformers import MarianMTModel, MarianTokenizer

            name = MARIAN[key]
            try:
                tok = MarianTokenizer.from_pretrained(name, local_files_only=True)
                mdl = MarianMTModel.from_pretrained(name, local_files_only=True)
            except Exception:
                # not in the local cache yet -> needs the network once
                try:
                    tok = MarianTokenizer.from_pretrained(name)
                    mdl = MarianMTModel.from_pretrained(name)
                except Exception as exc:
                    raise RuntimeError(
                        f"translation model '{name}' is not downloaded and the network "
                        f"is unavailable ({type(exc).__name__}). Connect once to fetch it."
                    ) from exc
            mdl.eval()
            _TRANS[key] = (tok, mdl)
        return _TRANS[key]


def _mt_batch(texts: list[str], src: str, tgt: str, batch_size: int = 32,
              beams: int = 2, on_progress=None, should_stop=None,
              on_batch=None) -> list[str]:
    import torch

    tok, mdl = _load_marian(src, tgt)
    torch.set_num_threads(4)
    out: list[str] = [""] * len(texts)
    # Longest-first batching keeps padding (and therefore compute) down.
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
    with torch.inference_mode():
        for start in range(0, len(order), batch_size):
            if should_stop and should_stop():
                raise Cancelled()
            idx = order[start:start + batch_size]
            chunk = [texts[i] if texts[i].strip() else "." for i in idx]
            enc = tok(chunk, return_tensors="pt", padding=True,
                      truncation=True, max_length=512)
            gen = mdl.generate(**enc, max_length=512, num_beams=beams,
                               do_sample=False)
            decoded = tok.batch_decode(gen, skip_special_tokens=True)
            for j, i in enumerate(idx):
                out[i] = decoded[j]
            if on_batch:
                on_batch(list(idx), list(decoded))
            if on_progress:
                on_progress(min(1.0, (start + len(idx)) / max(1, len(texts))))
    return out


_AR_RE = re.compile(r"[\u0600-\u06FF]")


def _plausible_arabic(src: str, out: str) -> bool:
    """Guard against Marian hallucinations on tiny/odd inputs."""
    out = out.strip()
    if not out:
        return False
    if not _AR_RE.search(out):
        return False
    arabic = len(_AR_RE.findall(out))
    # Protected glossary terms are deliberately Latin, so measure Arabic
    # density over the whole string rather than against Latin letters.
    return arabic >= 3 and arabic / max(1, len(out)) >= 0.15


# --------------------------------------------------------------------------- #
# Glossary / mixed-language protection
# --------------------------------------------------------------------------- #
# MT mangles foreign words and proper nouns ("bonjour" -> "بمجد"). We swap
# protected spans for {N} placeholders, which the model passes through intact,
# then restore them afterwards.

_PLACEHOLDER = re.compile(r"\{(\d+)\}")
_ACCENTED = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿŒœ]*[À-ÖØ-öø-ÿŒœ][A-Za-zÀ-ÖØ-öø-ÿŒœ]*")

# Unambiguous French words, for "keep foreign words" mode on mixed lessons.
FR_WORDS = {
    # Words that are unambiguously French. Deliberately excludes cognates that
    # are also English (alphabet, comment, phrase, pain, chat...) — protecting
    # those would leave Latin text inside English sentences.
    "bonjour", "bonsoir", "salut", "merci", "oui", "non", "ça", "très",
    "être", "avoir", "vous", "nous", "ils", "elles", "avec", "pour",
    "dans", "aux", "des", "une", "je", "tu", "il", "elle", "cette",
    "mon", "ton", "notre", "votre", "leur", "faire", "aller", "dire", "voir",
    "savoir", "pouvoir", "vouloir", "venir", "prendre", "mettre", "parler",
    "apprendre", "prononciation", "lettre", "lettres", "français",
    "française", "langue", "pourquoi", "quand", "voilà", "voici",
    "d'accord", "excusez", "pardon", "s'il", "plaît", "monsieur", "madame",
    "mademoiselle", "aussi", "encore", "maintenant", "aujourd'hui",
    "demain", "hier", "toujours", "jamais", "beaucoup", "assez",
}


def parse_glossary(raw) -> list[tuple[str, str]]:
    """Accept a list of strings or 'term = replacement' lines."""
    entries: list[tuple[str, str]] = []
    if not raw:
        return entries
    items = raw if isinstance(raw, (list, tuple)) else str(raw).splitlines()
    for line in items:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            term, _, rep = line.partition("=")
            term, rep = term.strip(), rep.strip()
        else:
            term, rep = line, line
        if term:
            entries.append((term, rep or term))
    # longest terms first so "bonjour tout le monde" wins over "bonjour"
    entries.sort(key=lambda x: len(x[0]), reverse=True)
    return entries


def _is_foreign_token(tok: str) -> bool:
    return tok.lower() in FR_WORDS or bool(_ACCENTED.search(tok))


def _collect_terms(text: str, glossary, keep_foreign: bool) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    low = text.lower()

    for term, rep in glossary:
        if term.lower() in low and term.lower() not in seen:
            found.append((term, rep))
            seen.add(term.lower())

    if keep_foreign:
        for tok in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿŒœ']+", text):
            if tok.lower() in seen or len(tok) < 2:
                continue
            if _is_foreign_token(tok):
                found.append((tok, tok))
                seen.add(tok.lower())

    found.sort(key=lambda x: len(x[0]), reverse=True)
    return found


def protect_text(text: str, terms: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """Replace terms with {N} placeholders. Returns (text, [(placeholder, replacement)])."""
    mapping: list[tuple[str, str]] = []
    out = text
    for term, rep in terms:
        if not term:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
        if not pattern.search(out):
            continue
        token = "{" + str(len(mapping)) + "}"
        out = pattern.sub(lambda _m, t=token: t, out, count=99)
        mapping.append((token, rep))
    return out, mapping


def restore_text(text: str, mapping: list[tuple[str, str]]) -> str:
    """Put the protected terms back; append any the model dropped."""
    missing = []
    out = text
    for token, rep in mapping:
        if token in out:
            out = out.replace(token, rep)
        else:
            missing.append(rep)
    if missing:
        out = out + " " + " ".join(missing)
    return out


def translate(texts: list[str], src: str, dst: str = "ar", log=None,
              on_progress=None, glossary=None, keep_foreign: bool = False,
              should_stop=None, on_batch=None) -> list[str]:
    """Translate a list of strings, chaining through English when needed.

    Terms in `glossary` (and French words when `keep_foreign`) are protected
    from the model and restored verbatim afterwards.
    """
    if src == dst:
        return list(texts)
    glossary = glossary or []

    protected, mappings = [], []
    for t in texts:
        terms = _collect_terms(t, glossary, keep_foreign)
        p, m = protect_text(t, terms)
        protected.append(p)
        mappings.append(m)

    if (src, dst) in MARIAN:
        if log:
            log(f"translating {len(texts)} segments {src}->{dst}")
        res = _mt_batch(protected, src, dst, on_progress=on_progress,
                        should_stop=should_stop, on_batch=on_batch)
    elif (src, "en") in MARIAN and ("en", dst) in MARIAN:
        if log:
            log(f"translating {len(texts)} segments {src}->en->{dst}")
        mid = _mt_batch(protected, src, "en", should_stop=should_stop)
        res = _mt_batch(mid, "en", dst, on_progress=on_progress,
                        should_stop=should_stop, on_batch=on_batch)
    else:
        raise RuntimeError(f"no translation route for {src}->{dst}")

    res = [restore_text(r, m) for r, m in zip(res, mappings)]

    fixed = 0
    for i, (s, t) in enumerate(zip(texts, res)):
        # cues with no letters at all (numbers, symbols) must not be "translated"
        if not re.search(r"[^\W\d_]", s):
            res[i] = s
            continue
        if not _plausible_arabic(s, t):
            res[i] = s
            fixed += 1
    if log and fixed:
        log(f"kept original for {fixed} segment(s) with implausible translation")
    return res


# --------------------------------------------------------------------------- #
# Output paths
# --------------------------------------------------------------------------- #

def resolve_output(video: str, *, mode: str = "beside", out_dir: str = "output",
                   suffix: str = ".ar", ext: str = ".srt") -> tuple[str, bool]:
    """Pick an output path. Returns (path, writable_beside)."""
    stem = os.path.splitext(video)[0]
    beside = stem + suffix + ext
    if mode == "beside":
        d = os.path.dirname(beside) or "."
        if os.access(d, os.W_OK):
            try:
                probe = os.path.join(d, ".write_test")
                with open(probe, "w"):
                    pass
                os.remove(probe)
                return beside, True
            except OSError:
                pass
        mode = "folder"
    rel = os.path.basename(video)
    target = os.path.join(out_dir, os.path.splitext(rel)[0] + suffix + ext)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    return target, False


def burn_in(video: str, srt: str, dest: str, *, font: str = "Arial",
            size: int = 22, log=None) -> None:
    """Hardsub via libass. Requires an Arabic-capable font installed."""
    vf = f"subtitles={srt}:force_style='FontName={font},FontSize={size}'"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
           "-c:a", "copy", dest]
    if log:
        log("burn-in: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
