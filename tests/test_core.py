"""Unit tests for the subtitle logic — no models, no network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core


def test_split_pair_keeps_total_time():
    text = "هذه جملة طويلة جدا تحتاج إلى تقسيم على عدة أسطر لعرضها على الشاشة بشكل مريح"
    cues = core.split_pair(0.0, 10.0, text, "a long line", max_chars=20)
    assert len(cues) >= 2
    assert cues[0].start == 0.0
    assert abs(cues[-1].end - 10.0) < 0.01
    # cues stay ordered and non-empty
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start + 0.01
    assert all(c.text.strip() for c in cues)


def test_short_pair_is_untouched():
    cues = core.split_pair(1.0, 2.0, "مرحبا", "hello")
    assert len(cues) == 1
    assert cues[0].start == 1.0 and cues[0].end == 2.0


def test_merge_tiny_is_capped():
    cues = [core.Cue(i * 0.7, i * 0.7 + 0.5, f"w{i}") for i in range(60)]
    merged = core.merge_tiny(cues)
    assert all(c.end - c.start <= 7.01 for c in merged)
    assert len(merged) < len(cues)


def test_srt_roundtrip(tmp_path):
    cues = [core.Cue(0.0, 1.5, "مرحبا"), core.Cue(1.6, 3.0, "بالعالم")]
    path = tmp_path / "out.srt"
    core.write_srt(cues, str(path), bom=True)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")          # BOM for TVs
    text = raw.decode("utf-8-sig")
    assert "00:00:00,000 --> 00:00:01,500" in text
    assert "مرحبا" in text


def test_qc_flags_and_fixes_long_cue():
    cues = [core.Cue(0, 30, "this cue stays on screen far too long for a viewer to read"),
            core.Cue(30.2, 30.4, "yes")]
    assert any(i.kind == "too_long" for i in core.qc_check(cues))
    fixed = core.qc_fix(cues)
    assert max(c.end - c.start for c in fixed) <= 7.01
    assert core.qc_check(fixed) == []


def test_qc_detects_overlap_and_reading_speed():
    cues = [core.Cue(0, 3, "one two three"), core.Cue(2.0, 2.4, "x" * 40)]
    kinds = {i.kind for i in core.qc_check(cues)}
    assert "overlap" in kinds
    assert "too_fast" in kinds


def test_glossary_is_protected_and_restored():
    terms = core.parse_glossary("Caillou\nbonjour = مرحبا")
    assert ("bonjour", "مرحبا") in terms
    protected, mapping = core.protect_text("Caillou says bonjour", terms)
    assert "Caillou" not in protected and "bonjour" not in protected
    restored = core.restore_text(protected, mapping)
    assert "Caillou" in restored and "مرحبا" in restored


def test_clean_ar_removes_duplicates_and_stray_quotes():
    assert core.clean_ar('مرحبا "ديلان"') == "مرحبا ديلان"
    assert core.clean_ar("كلمة كلمة ثانية") == "كلمة ثانية"


def test_cues_from_words_breaks_on_sentence_end():
    class W:
        def __init__(self, word, start, end):
            self.word, self.start, self.end = word, start, end

    words = [W(" Hello", 0, 0.4), W(" world.", 0.4, 0.9), W(" Next", 1.2, 1.5)]
    cues = core.cues_from_words(words)
    assert len(cues) == 2
    assert cues[0].text == "Hello world."
    assert abs(cues[1].start - 1.2) < 0.01
