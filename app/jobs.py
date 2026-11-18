"""Job queue: one worker thread, resumable, persisted to disk."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid

from . import core

DEFAULTS = {
    "model": "small",
    "source_lang": "auto",
    "translate": True,
    "bilingual": False,
    "format": "srt",          # srt | ass | both
    "naming": "lang_suffix",  # lang_suffix -> name.ar.srt | plain -> name.srt
    "output_mode": "beside",  # beside | folder
    "out_dir": "output",
    "bom": True,
    "rtl_marks": False,
    "skip_existing": True,
    "threads": 4,
    "beam_size": 5,
    "max_chars": 42,
    "burn_in": False,
    "font": "Arial",
    "font_size": 22,
    "glossary": [],
    "keep_foreign": False,
    "prompt": "",
}


class Store:
    """Tiny JSON-backed state, guarded by a lock."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"jobs": [], "settings": dict(DEFAULTS)}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data["jobs"] = loaded.get("jobs", [])
                self.data["settings"].update(loaded.get("settings", {}))
            except Exception:
                pass

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    def jobs(self):
        with self.lock:
            return self.data["jobs"]

    def job(self, jid):
        with self.lock:
            for j in self.data["jobs"]:
                if j["id"] == jid:
                    return j
        return None


class Worker:
    def __init__(self, store: Store, base_dir: str):
        self.store = store
        self.base_dir = base_dir
        self.thread: threading.Thread | None = None
        self.pause = threading.Event()
        self.cancel = threading.Event()
        self._current: str | None = None
        self._started = 0.0
        self._audio_done = 0.0

    # -- lifecycle ---------------------------------------------------------- #
    def ensure_running(self):
        if self.thread and self.thread.is_alive():
            return
        self.pause.clear()
        self.cancel.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="subs-worker")
        self.thread.start()

    def _next_job(self):
        with self.store.lock:
            for j in self.store.data["jobs"]:
                if j["status"] in ("queued", "running"):
                    return j
        return None

    def _loop(self):
        while not self.cancel.is_set():
            job = self._next_job()
            if job is None:
                return
            with self.store.lock:
                job["status"] = "running"
                job.setdefault("started_at", time.time())
            self.store.save()
            self._run_job(job)
            if self.cancel.is_set():
                with self.store.lock:
                    job["status"] = "cancelled"
                self.store.save()
                return
            with self.store.lock:
                if job.get("status") == "cancelled":
                    job["finished_at"] = time.time()
                else:
                    statuses = [it["status"] for it in job["items"]]
                    job["status"] = ("done" if all(s in ("done", "skipped", "error") for s in statuses)
                                     else "paused")
                    job["finished_at"] = time.time()
            self.store.save()

    def _job_cancelled(self, job) -> bool:
        with self.store.lock:
            return job.get("status") == "cancelled"

    def _run_job(self, job):
        opts = job["options"]
        for idx, item in enumerate(job["items"]):
            if self.cancel.is_set() or self._job_cancelled(job):
                return
            while self.pause.is_set():
                time.sleep(0.4)
                if self.cancel.is_set() or self._job_cancelled(job):
                    return
            if item["status"] in ("done", "skipped", "cancelled"):
                continue
            with self.store.lock:
                job["current"] = idx
                item["status"] = "running"
                item["started_at"] = time.time()
            self.store.save()
            try:
                self._process(job, item, opts)
            except core.Cancelled:
                with self.store.lock:
                    item["status"] = "cancelled"
                    item["phase"] = "cancelled"
                    item["error"] = None
                self.store.save()
                return
            except Exception as exc:  # noqa: BLE001 - report per item
                with self.store.lock:
                    item["status"] = "error"
                    item["error"] = f"{type(exc).__name__}: {exc}"
                    item["trace"] = traceback.format_exc(limit=4)
                self.store.save()
            with self.store.lock:
                job["eta"] = self._eta(job)
            self.store.save()

    # -- one video ---------------------------------------------------------- #
    def _process(self, job, item, opts):
        video = item["path"]

        def stopped() -> bool:
            return self._job_cancelled(job) or self.cancel.is_set()
        started = time.time()
        duration = item.get("duration") or core.probe_duration(video)

        if opts.get("skip_existing"):
            suffix = ".ar" if opts.get("naming") == "lang_suffix" else ""
            stem = os.path.splitext(video)[0]
            for ext in (".srt", ".ass"):
                cand = stem + suffix + ext
                if os.path.exists(cand) and os.path.getsize(cand) > 0:
                    with self.store.lock:
                        item["status"] = "skipped"
                        item["out"] = cand
                        item["elapsed"] = round(time.time() - started, 1)
                    self.store.save()
                    return

        lang = opts.get("source_lang", "auto")
        lang = None if lang in ("auto", "", None) else lang

        def on_progress(frac: float):
            with self.store.lock:
                item["phase"] = "transcribe"
                item["progress"] = round(frac * 0.95, 4)
                job["audio_done"] = round(
                    sum((i.get("duration") or 0) * (1.0 if i["status"] in ("done", "skipped") else i.get("progress", 0))
                        for i in job["items"]), 1)
                job["eta"] = self._eta(job)
            # persist sparsely to limit i/o
            if int(frac * 100) % 5 == 0:
                self.store.save()

        def on_translate(frac: float):
            with self.store.lock:
                item["phase"] = "translate"
                item["progress"] = round(0.95 + 0.04 * frac, 4)
            if int(frac * 100) % 10 == 0:
                self.store.save()

        # Bias the recogniser towards the lesson vocabulary: the glossary the
        # user wrote for translation doubles as an ASR hint.
        glossary = core.parse_glossary(opts.get("glossary"))
        hotwords = ", ".join(t for t, _ in glossary[:60]) or None
        prompt = (opts.get("prompt") or "").strip()
        if not prompt and hotwords:
            prompt = f"The speaker mentions these words: {hotwords}."

        cues, info = core.transcribe(
            video,
            size=opts.get("model", "small"),
            language=lang,
            threads=opts.get("threads", 4),
            models_dir=os.path.join(self.base_dir, "models"),
            beam_size=opts.get("beam_size", 5),
            max_chars=opts.get("max_chars", 42),
            on_progress=on_progress,
            initial_prompt=prompt or None,
            hotwords=hotwords,
            should_stop=stopped,
        )
        src = (info.language or lang or "en").lower()
        max_chars = opts.get("max_chars", 42)

        # Translate whole ASR segments, then split for display. Translating
        # pre-split fragments is what produces broken Arabic.
        final: list[core.Cue] = []
        if opts.get("translate", True) and src != "ar":
            texts = [c.text for c in cues]
            ar = core.translate(
                texts, src, "ar",
                on_progress=on_translate,
                glossary=core.parse_glossary(opts.get("glossary")),
                keep_foreign=bool(opts.get("keep_foreign")),
                should_stop=stopped,
            )
            for c, t in zip(cues, ar):
                final.extend(core.split_pair(c.start, c.end, t, c.text,
                                             max_chars=max_chars))
        else:
            for c in cues:
                final.extend(core.split_pair(c.start, c.end, c.text, "",
                                             max_chars=max_chars))
        cues = core.merge_tiny(final)
        if stopped():
            raise core.Cancelled()
        with self.store.lock:
            item["phase"] = "writing"
            item["progress"] = 0.995
        self.store.save()

        base_out, _beside = core.resolve_output(
            video,
            mode=opts.get("output_mode", "beside"),
            out_dir=os.path.join(self.base_dir, opts.get("out_dir", "output")),
            suffix=".ar" if opts.get("naming") == "lang_suffix" else "",
            ext=".srt",
        )
        stem = base_out[:-4]

        fmt = opts.get("format", "srt")
        written = []
        if fmt in ("srt", "both"):
            core.write_srt(cues, stem + ".srt", bom=opts.get("bom", True),
                           rtl_marks=opts.get("rtl_marks", False),
                           bilingual=opts.get("bilingual", False),
                           max_chars=opts.get("max_chars", 42))
            written.append(stem + ".srt")
        if fmt in ("ass", "both"):
            core.write_ass(cues, stem + ".ass", font=opts.get("font", "Arial"),
                           size=opts.get("font_size", 22) * 2,
                           bilingual=opts.get("bilingual", False),
                           max_chars=opts.get("max_chars", 42))
            written.append(stem + ".ass")

        if opts.get("burn_in"):
            dest = stem + ".burned.mp4"
            core.burn_in(video, stem + ".srt", dest,
                         font=opts.get("font", "Arial"),
                         size=opts.get("font_size", 22))
            written.append(dest)

        with self.store.lock:
            item["status"] = "done"
            item["progress"] = 1.0
            item["out"] = written[0] if written else None
            item["outputs"] = written
            item["lang"] = src
            item["elapsed"] = round(time.time() - started, 1)
            item["segments"] = len(cues)
        self.store.save()

    def _eta(self, job) -> float | None:
        """Seconds left, estimated only from work done in this run.

        Wall-clock since the job was created is useless — the job may have sat
        queued or offline for hours — so the rate comes from items that actually
        consumed time and still have audio left.
        """
        items = job["items"]
        now = time.time()
        remaining = 0.0
        rates: list[float] = []
        for i in items:
            dur = i.get("duration") or 0
            st = i.get("status")
            elapsed = i.get("elapsed") or 0
            if st in ("done", "skipped"):
                if elapsed > 0 and dur > 0:
                    rates.append(dur / elapsed)
                continue
            if st == "running":
                el = now - (i.get("started_at") or now)
                prog = i.get("progress") or 0
                if el > 0 and prog > 0 and dur > 0:
                    rates.append(dur * prog / el)
                remaining += dur * (1 - prog)
            else:
                remaining += dur
        if not rates or remaining <= 0:
            return 0.0 if remaining <= 0 else None
        recent = rates[-8:]
        rate = sum(recent) / len(recent)
        return round(remaining / rate) if rate > 0 else None

    # -- controls ----------------------------------------------------------- #
    def pause_all(self):
        self.pause.set()

    def resume_all(self):
        self.pause.clear()
        self.ensure_running()

    def cancel_all(self):
        self.cancel.set()
        self.pause.clear()


def new_job(store: Store, paths: list[str], options: dict) -> dict | None:
    opts = dict(DEFAULTS)
    opts.update(store.data.get("settings", {}))
    opts.update({k: v for k, v in options.items() if v is not None})

    # don't queue the same file twice in parallel
    active = set()
    with store.lock:
        for j in store.data["jobs"]:
            if j["status"] in ("queued", "running", "paused"):
                for it in j["items"]:
                    if it["status"] in ("pending", "running"):
                        active.add(it["path"])
    paths = [p for p in paths if p not in active]
    if not paths:
        return None

    items = []
    for p in paths:
        items.append({
            "path": p,
            "name": os.path.basename(p),
            "duration": round(core.probe_duration(p), 2),
            "status": "pending",
            "progress": 0.0,
            "out": None,
            "error": None,
        })
    job = {
        "id": uuid.uuid4().hex[:12],
        "created_at": time.time(),
        "status": "queued",
        "options": opts,
        "items": items,
        "current": 0,
        "audio_done": 0,
        "eta": None,
    }
    with store.lock:
        store.data["jobs"].insert(0, job)
        # keep history bounded
        store.data["jobs"] = store.data["jobs"][:60]
    store.save()
    return job
