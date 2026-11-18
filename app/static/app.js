const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

let LIB = { root: null, items: [] };
let SELECTED = new Set();
let LAST_JOB = null;
let EDIT = { path: null, subPath: null, cues: [] };
let PREVIEW = { cues: [] };

/* ---------------------------------------------------------------- helpers */
const fmtDur = (s) => {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
};
const fmtBytes = (b) => {
  if (!b) return "0";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b < 10 ? 1 : 0)} ${u[i]}`;
};

function toast(msg, kind = "") {
  const t = el("div", "toast " + kind, msg);
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 3200);
  setTimeout(() => t.remove(), 3600);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let detail = await r.text();
    try { detail = JSON.parse(detail).detail || JSON.parse(detail).error || detail; } catch (e) {}
    throw new Error(detail.slice(0, 160));
  }
  return r.json();
}

/* SRT time helpers */
const pad = (n, w = 2) => String(n).padStart(w, "0");
function toSrt(t) {
  const ms = Math.round(t * 1000);
  const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000), r = ms % 1000;
  return `${pad(h)}:${pad(m)}:${pad(s)},${pad(r, 3)}`;
}
function parseSrtTime(str) {
  const m = str.trim().replace(",", ".").match(/(\d+):(\d+):(\d+(?:\.\d+)?)/);
  if (!m) return null;
  return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
}
function parseSrt(text) {
  const blocks = text.replace(/\r\n/g, "\n").split(/\n{2,}/);
  const cues = [];
  for (const b of blocks) {
    const lines = b.trim().split("\n").filter((l) => l !== "");
    if (lines.length < 2) continue;
    let i = 0;
    if (/^\d+$/.test(lines[0].trim())) i = 1;
    const arrow = lines[i] && lines[i].includes("-->");
    if (!arrow) continue;
    const [a, b2] = lines[i].split("-->");
    const st = parseSrtTime(a), en = parseSrtTime(b2);
    if (st == null || en == null) continue;
    cues.push({ start: st, end: en, text: lines.slice(i + 1).join("\n") });
  }
  return cues;
}
function buildSrt(cues) {
  return cues.map((c, i) =>
    `${i + 1}\n${toSrt(c.start)} --> ${toSrt(c.end)}\n${c.text.trim()}\n`).join("\n");
}

/* ---------------------------------------------------------------- health */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const chips = $("#chips");
    chips.innerHTML = "";
    const add = (label, ok) => chips.appendChild(
      el("span", "chip " + (ok ? "ok" : "bad"), label));
    add("ffmpeg", h.ffmpeg);
    add("whisper", h.faster_whisper);
    add("translator", h.transformers);
    const models = (h.models_on_disk || []).filter((m) => !m.startsWith("."));
    if (models.length) chips.appendChild(el("span", "chip", `${models.length} model${models.length > 1 ? "s" : ""} ready`));
  } catch (e) { /* server starting */ }
}

/* ---------------------------------------------------------------- scan */
async function scan() {
  const root = $("#path").value.trim();
  if (!root) return;
  $("#list").innerHTML = `<div class="empty">Scanning <strong>${root}</strong>…</div>`;
  try {
    const res = await api("/api/scan", { method: "POST", body: JSON.stringify({ root, recursive: true }) });
    LIB = { root: res.root, items: res.items };
    SELECTED = new Set();
    renderList();
    toast(`${res.count} videos · ${res.hours} h`, "ok");
  } catch (e) {
    $("#list").innerHTML = `<div class="empty"><strong>Cannot scan that folder</strong>${e.message}<br>
      Press “Allow” to add it to the allowed roots.</div>`;
  }
}

async function allowRoot() {
  const root = $("#path").value.trim();
  if (!root) return;
  try {
    await api("/api/add_root", { method: "POST", body: JSON.stringify({ root }) });
    await scan();
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------------------------------------------------------- list */
function filtered() {
  const q = $("#search").value.toLowerCase().trim();
  const hideDone = $("#hideDone").checked;
  const needAudio = $("#noAudio").checked;
  return LIB.items.filter((it) => {
    if (q && !it.name.toLowerCase().includes(q)) return false;
    if (hideDone && it.has_ar) return false;
    if (needAudio && !it.has_audio) return false;
    return true;
  });
}

function renderList() {
  const list = $("#list");
  const items = filtered();
  list.innerHTML = "";
  if (!LIB.items.length) {
    list.innerHTML = `<div class="empty"><strong>No videos found</strong>Try another folder.</div>`;
    $("#stats").textContent = "—";
    $("#selInfo").textContent = "";
    return;
  }
  const totalH = LIB.items.reduce((a, i) => a + i.duration, 0) / 3600;
  $("#stats").textContent = `${LIB.items.length} files · ${totalH.toFixed(1)} h`;

  for (const it of items) {
    const row = el("div", "item" + (SELECTED.has(it.path) ? " sel" : ""));
    const cb = el("input"); cb.type = "checkbox"; cb.checked = SELECTED.has(it.path);
    cb.onclick = (ev) => { ev.stopPropagation(); toggle(it.path, cb.checked); row.classList.toggle("sel", cb.checked); };
    row.appendChild(cb);

    const mid = el("div");
    mid.appendChild(el("div", "name", it.name));
    const meta = el("div", "meta");
    meta.appendChild(el("span", null, fmtDur(it.duration)));
    meta.appendChild(el("span", null, fmtBytes(it.size)));
    mid.appendChild(meta);
    row.appendChild(mid);

    const badges = el("div", "badges");
    if (it.has_ar) badges.appendChild(el("span", "badge ar", "AR"));
    if (!it.has_audio) badges.appendChild(el("span", "badge noaudio", "no audio"));
    row.appendChild(badges);

    row.onclick = () => { const v = !SELECTED.has(it.path); toggle(it.path, v); cb.checked = v; row.classList.toggle("sel", v); };
    list.appendChild(row);
  }
  updateSelInfo();
}

function toggle(p, on) { if (on) SELECTED.add(p); else SELECTED.delete(p); updateSelInfo(); }

function updateSelInfo() {
  const sel = LIB.items.filter((i) => SELECTED.has(i.path));
  const dur = sel.reduce((a, i) => a + i.duration, 0);
  $("#selInfo").textContent = sel.length ? `${sel.length} selected · ${(dur / 3600).toFixed(1)} h` : "";
}

/* ---------------------------------------------------------------- job */
function options() {
  return {
    model: $("#model").value,
    source_lang: $("#srclang").value,
    translate: $("#translate").checked,
    bilingual: $("#bilingual").checked,
    format: $("#format").value,
    naming: $("#naming").value,
    output_mode: $("#outputmode").value,
    bom: $("#bom").checked,
    rtl_marks: $("#rtl").checked,
    skip_existing: $("#skip").checked,
    burn_in: $("#burn").checked,
    prompt: $("#prompt").value,
    threads: parseInt($("#threads").value) || 4,
  };
}

async function startBatch() {
  const paths = LIB.items.filter((i) => SELECTED.has(i.path)).map((i) => i.path);
  if (!paths.length) { toast("Select at least one video first", "err"); return; }
  const hours = LIB.items.filter((i) => SELECTED.has(i.path)).reduce((a, i) => a + i.duration, 0) / 3600;
  const rate = { small: 2.9, base: 5, tiny: 8 }[$("#model").value] || 2.9;
  const eta = (hours / rate) * 3600;
  if (!confirm(`Queue ${paths.length} file(s) — ${hours.toFixed(1)} h of audio.\n\nEstimated compute: ~${fmtDur(eta)}.\n\nStart?`)) return;
  try {
    const res = await api("/api/jobs", { method: "POST", body: JSON.stringify({ paths, options: options() }) });
    LAST_JOB = res.job.id;
    toast(`Queued ${paths.length} file(s)`, "ok");
    refreshJobs();
  } catch (e) { toast(e.message, "err"); }
}

async function control(action) {
  try {
    if (action === "cancel" && LAST_JOB && !confirm("Cancel the running batch?")) return;
    await api(`/api/jobs/${LAST_JOB}/${action}`, { method: "POST" });
    refreshJobs();
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------------------------------------------------------- queue */
function renderQueue(data) {
  const jobs = data.jobs || [];
  const box = $("#queue");
  if (!jobs.length) {
    box.innerHTML = `<div class="empty"><strong>No jobs yet</strong>Select videos and press start.</div>`;
    $("#qsum").textContent = "";
    return;
  }
  const j = jobs[0];
  LAST_JOB = j.id;
  const done = j.items.filter((i) => ["done", "skipped"].includes(i.status)).length;
  const err = j.items.filter((i) => i.status === "error").length;
  const totalDur = j.items.reduce((a, i) => a + (i.duration || 0), 0);
  const finished = (st) => st === "done" || st === "skipped";
  const doneDur = j.items.reduce(
    (a, i) => a + (i.duration || 0) * (finished(i.status) ? 1 : (i.progress || 0)), 0);
  const overall = totalDur ? Math.round((doneDur / totalDur) * 100) : Math.round(((done / j.items.length) || 0) * 100);
  $("#qsum").textContent =
    `${done}/${j.items.length} · ${overall}%${err ? ` · ${err} failed` : ""}${j.eta ? ` · ${fmtDur(j.eta)} left` : ""}`;

  box.innerHTML = "";
  for (const it of j.items) {
    const row = el("div", "qitem");
    const h = el("div", "qhead");
    h.appendChild(el("span", "dot " + it.status));
    const nm = el("span", "qname", it.name);
    nm.title = it.path;
    nm.onclick = () => preview(it);
    h.appendChild(nm);
    const isDone = it.status === "done" || it.status === "skipped";
    const pct = Math.round((isDone ? 1 : (it.progress || 0)) * 100);
    const pctEl = el("span", "pct" + (isDone ? " ok" : it.status === "running" ? " live" : ""), `${pct}%`);
    h.appendChild(pctEl);
    row.appendChild(h);

    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill " + ((it.status === "done" || it.status === "skipped") ? "done" : it.status === "error" ? "error" : it.status === "running" ? "running" : ""));
    const complete = it.status === "done" || it.status === "skipped";
    fill.style.width = `${((complete ? 1 : (it.progress || 0)) * 100).toFixed(1)}%`;
    track.appendChild(fill);
    row.appendChild(track);

    const meta = el("div", "qmeta");
    meta.appendChild(el("span", null, it.phase && it.status === "running" ? it.phase : it.status));
    if (it.lang) meta.appendChild(el("span", null, it.lang));
    if (it.segments) meta.appendChild(el("span", null, `${it.segments} cues`));
    if (it.elapsed) meta.appendChild(el("span", null, `${it.elapsed}s`));
    if (it.error) meta.appendChild(el("span", null, it.error));
    else if (it.status === "done" || it.status === "skipped") {
      const pv = el("button", "btn sm ghost", "Preview");
      pv.onclick = (ev) => { ev.stopPropagation(); preview(it); };
      meta.appendChild(pv);
      const dirOf = (p) => (p || "").split("/").slice(0, -1).join("/");
      if (it.out && dirOf(it.out) !== dirOf(it.path)) {
        const warn = el("span", "badge", "in output/");
        warn.title = "The video folder is read-only, so the subtitle was saved to output/ — copy it next to the video.";
        meta.appendChild(warn);
      }
      const b = el("button", "btn sm ghost edit", "Edit subtitle");
      b.onclick = (ev) => { ev.stopPropagation(); openEditor(it.path, it.out); };
      meta.appendChild(b);
    }
    row.appendChild(meta);
    box.appendChild(row);
  }
}

async function refreshJobs() {
  try { renderQueue(await api("/api/jobs")); } catch (e) {}
}

/* ---------------------------------------------------------------- preview */
async function preview(item) {
  $("#previewModal").classList.add("on");
  $("#pname").textContent = item.name;
  const v = $("#player");
  if (v.dataset.src !== item.path) {
    v.src = `/api/media?path=${encodeURIComponent(item.path)}`;
    v.dataset.src = item.path;
    v.load();
  }
  $("#subtext").textContent = "loading…";
  PREVIEW.cues = [];
  try {
    const q = new URLSearchParams({ path: item.path });
    if (item.out) q.set("sub", item.out);
    const r = await api(`/api/subtitle?${q}`);
    PREVIEW.cues = parseSrt(r.text);
    const v1 = $("#player");
    if (PREVIEW.cues.length && v1.readyState >= 1) {
      v1.currentTime = Math.max(0, PREVIEW.cues[0].start);
    }
    renderPreviewSub();
  } catch (e) {
    $("#subtext").textContent = "No subtitle yet for this file.";
  }
}

/* show the cue that matches the video position */
function renderPreviewSub() {
  const v = $("#player");
  const t = v.currentTime || 0;
  const active = PREVIEW.cues.filter((c) => t >= c.start && t <= c.end);
  if (!active.length) {
    const next = PREVIEW.cues.find((c) => c.start > t);
    $("#subtext").textContent = next ? "…" : "—";
    return;
  }
  $("#subtext").textContent = active.map((c) => c.text).join("\n\n");
}

/* ---------------------------------------------------------------- editor */
async function openEditor(path, subPath) {
  try {
    const q = new URLSearchParams({ path });
    if (subPath) q.set("sub", subPath);
    const r = await api(`/api/subtitle?${q}`);
    EDIT = { path, subPath: r.path, cues: parseSrt(r.text) };
  } catch (e) { toast("No subtitle to edit yet", "err"); return; }

  $("#edTitle").textContent = path.split("/").pop();
  $("#edShift").value = "0";
  EDIT.active = -1;
  EDIT.stopAt = null;
  const v = $("#edVideo");
  const src = `/api/media?path=${encodeURIComponent(path)}`;
  if (v.dataset.src !== src) { v.src = src; v.dataset.src = src; }
  $("#edNow").textContent = "Click a line to hear it, then fix the text.";
  renderCues();
  $("#scrim").classList.add("on");
  $("#drawer").classList.add("on");
}

function renderCues() {
  const box = $("#cues");
  box.innerHTML = "";
  EDIT.cues.forEach((c, i) => {
    const row = el("div", "cue" + (EDIT.active === i ? " active" : ""));
    row.appendChild(el("span", "idx", i + 1));

    const right = el("div");
    const times = el("div", "times");
    const st = el("input"); st.value = toSrt(c.start); st.spellcheck = false;
    st.onchange = () => { const v = parseSrtTime(st.value); if (v != null) c.start = v; else st.value = toSrt(c.start); };
    const en = el("input"); en.value = toSrt(c.end); en.spellcheck = false;
    en.onchange = () => { const v = parseSrtTime(en.value); if (v != null) c.end = v; else en.value = toSrt(c.end); };
    const play = el("button", "play", "▶ play");
    play.onclick = (ev) => { ev.stopPropagation(); playCue(i); };
    times.append(st, el("span", null, "→"), en, play);
    right.appendChild(times);

    const ta = el("textarea", "ar");
    ta.value = c.text;
    ta.rows = Math.min(4, Math.max(1, c.text.split("\n").length));
    ta.oninput = () => { c.text = ta.value; };
    right.appendChild(ta);

    row.appendChild(right);
    row.onclick = (ev) => {
      if (ev.target.tagName === "TEXTAREA" || ev.target.tagName === "INPUT") return;
      playCue(i);
    };
    box.appendChild(row);
  });
  box.scrollTop = 0;
}

function playCue(i) {
  const c = EDIT.cues[i];
  const v = $("#edVideo");
  if (!c || !v.src) return;
  EDIT.active = i;
  EDIT.stopAt = c.end;
  v.currentTime = Math.max(0, c.start);
  v.play().catch(() => {});
  document.querySelectorAll(".cue").forEach((r, k) => r.classList.toggle("active", k === i));
  document.querySelectorAll(".cue")[i]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  $("#edNow").textContent = `cue ${i + 1} · ${toSrt(c.start)} → ${toSrt(c.end)} — listening…`;
  const ta = document.querySelectorAll(".cue textarea")[i];
  if (ta) ta.focus({ preventScroll: true });
}

function applyShift() {
  const d = parseFloat($("#edShift").value) || 0;
  if (!d) return;
  EDIT.cues.forEach((c) => {
    c.start = Math.max(0, c.start + d);
    c.end = Math.max(0.05, c.end + d);
  });
  renderCues();
  toast(`Shifted all cues by ${d > 0 ? "+" : ""}${d}s`, "ok");
}

async function saveEditor() {
  try {
    await api("/api/subtitle", {
      method: "PUT",
      body: JSON.stringify({ path: EDIT.subPath, text: buildSrt(EDIT.cues) }),
    });
    toast(`Saved ${EDIT.cues.length} cues`, "ok");
    const it = LIB.items.find((i) => i.path === EDIT.path);
    if (it) it.has_ar = true;
    const v = $("#player");
    if (v.dataset.src === EDIT.path) preview({ path: EDIT.path, name: $("#edTitle").textContent });
    renderList();
  } catch (e) { toast("Save failed: " + e.message, "err"); }
}

function closeEditor() {
  const v = $("#edVideo");
  if (v) { v.pause(); EDIT.stopAt = null; }
  $("#drawer").classList.remove("on");
  if (!$("#sdrawer").classList.contains("on")) $("#scrim").classList.remove("on");
}

function openSettings() {
  $("#sdrawer").classList.add("on");
  $("#scrim").classList.add("on");
}

function closeSettings() {
  $("#sdrawer").classList.remove("on");
  if (!$("#drawer").classList.contains("on")) $("#scrim").classList.remove("on");
}

function closePreview() {
  const v = $("#player");
  if (v) v.pause();
  $("#previewModal").classList.remove("on");
}

function closeAll() {
  closePreview();
  $("#drawer").classList.remove("on");
  $("#sdrawer").classList.remove("on");
  $("#scrim").classList.remove("on");
}

/* ---------------------------------------------------------------- events */
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => { try { renderQueue(JSON.parse(ev.data)); } catch (e) {} };
}

/* ---------------------------------------------------------------- wire */
$("#scan").onclick = scan;
$("#pick").onclick = allowRoot;
$("#examples").onclick = () => { $("#path").value = "examples"; scan(); };
$("#path").addEventListener("keydown", (e) => { if (e.key === "Enter") scan(); });
$("#search").oninput = renderList;
$("#hideDone").onchange = renderList;
$("#noAudio").onchange = renderList;
$("#all").onclick = () => { filtered().forEach((i) => SELECTED.add(i.path)); renderList(); };
$("#none").onclick = () => { SELECTED.clear(); renderList(); };
$("#selNosub").onclick = () => { filtered().forEach((i) => { if (!i.has_ar) SELECTED.add(i.path); }); renderList(); };
$("#start").onclick = startBatch;
$("#pause").onclick = () => control("pause");
$("#resume").onclick = () => control("resume");
$("#cancel").onclick = () => control("cancel");

$("#scrim").onclick = closeAll;
$("#pClose").onclick = closePreview;
$("#previewModal").onclick = (ev) => { if (ev.target.id === "previewModal") closePreview(); };
$("#player").addEventListener("timeupdate", renderPreviewSub);
$("#player").addEventListener("seeked", renderPreviewSub);
$("#player").addEventListener("loadedmetadata", () => {
  const v = $("#player");
  if (PREVIEW.cues.length && v.currentTime === 0) {
    v.currentTime = Math.max(0, PREVIEW.cues[0].start);
  }
});
$("#edClose").onclick = closeEditor;
$("#openSettings").onclick = openSettings;
$("#sClose").onclick = closeSettings;
$("#edSave").onclick = saveEditor;
$("#edShiftApply").onclick = applyShift;
$("#edReplay").onclick = () => { if (EDIT.active >= 0) playCue(EDIT.active); };
$("#edVideo").addEventListener("timeupdate", () => {
  const v = $("#edVideo");
  if (EDIT.stopAt != null && v.currentTime >= EDIT.stopAt) {
    v.pause();
    EDIT.stopAt = null;
    $("#edNow").textContent = "paused — rewrite the line, then press ▶ play to compare";
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAll();
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && $("#drawer").classList.contains("on")) {
    e.preventDefault(); saveEditor();
  }
});

const deepEdit = new URLSearchParams(location.search).get("edit");
if (deepEdit) setTimeout(() => openEditor(deepEdit), 400);

loadHealth();
refreshJobs();
connectEvents();
scan();

/* ---------------------------------------------------------------- theme */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("theme", t);
  const b = $("#theme");
  if (b) b.textContent = t === "light" ? "☾" : "☀";
}
$("#theme").onclick = () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
};
applyTheme(localStorage.getItem("theme")
  || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

/* ---------------------------------------------------------------- folder picker */
let BROWSER = { path: null, parent: null, entries: [], selected: new Set() };

async function openBrowse() {
  $("#browseModal").classList.add("on");
  await browseTo(null);
}
function closeBrowse() { $("#browseModal").classList.remove("on"); }

async function browseTo(path) {
  try {
    const r = await api("/api/browse" + (path ? `?path=${encodeURIComponent(path)}` : ""));
    BROWSER = { path: r.path, parent: r.parent, entries: r.entries, selected: new Set() };
    $("#bPath").value = r.path;
    $("#bUp").disabled = !r.parent;
    renderBrowse();
  } catch (e) { toast(e.message, "err"); }
}

function renderBrowse() {
  const box = $("#bList");
  box.innerHTML = "";
  const dirs = BROWSER.entries.filter((e) => e.type === "dir");
  const vids = BROWSER.entries.filter((e) => e.type === "video");

  if (!dirs.length && !vids.length) {
    box.innerHTML = `<div class="empty"><strong>Empty folder</strong>No sub-folders or videos here.</div>`;
  }

  for (const d of dirs) {
    const row = el("div", "brow-row dir");
    row.innerHTML = `<span class="ico">▸</span>`;
    row.appendChild(el("span", "bname", d.name));
    row.onclick = () => browseTo(d.path);
    box.appendChild(row);
  }
  for (const v of vids) {
    const row = el("div", "brow-row");
    const cb = el("input"); cb.type = "checkbox";
    cb.onclick = (ev) => {
      ev.stopPropagation();
      if (cb.checked) BROWSER.selected.add(v.path); else BROWSER.selected.delete(v.path);
      updateBrowseInfo();
    };
    row.appendChild(cb);
    row.appendChild(el("span", "bname", v.name));
    row.appendChild(el("span", "bsize", fmtBytes(v.size)));
    row.onclick = () => { cb.checked = !cb.checked; cb.onclick({ stopPropagation() {} }); };
    box.appendChild(row);
  }
  updateBrowseInfo();
}

function updateBrowseInfo() {
  const n = BROWSER.selected.size;
  const vids = BROWSER.entries.filter((e) => e.type === "video").length;
  $("#bInfo").textContent = n ? `${n} selected` : `${vids} video${vids === 1 ? "" : "s"} in this folder`;
  $("#bAddVideos").disabled = n === 0;
}

async function useFolder() {
  try {
    await api("/api/add_root", { method: "POST", body: JSON.stringify({ root: BROWSER.path }) });
    $("#path").value = BROWSER.path;
    closeBrowse();
    await scan();
  } catch (e) { toast(e.message, "err"); }
}

async function addSelectedVideos() {
  const picks = [...BROWSER.selected];
  if (!picks.length) return;
  try {
    await api("/api/add_root", { method: "POST", body: JSON.stringify({ root: BROWSER.path }) });
    $("#path").value = BROWSER.path;
    closeBrowse();
    await scan();
    SELECTED = new Set(picks);
    renderList();
    toast(`${picks.length} video${picks.length === 1 ? "" : "s"} selected`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

$("#browse").onclick = openBrowse;
$("#bClose").onclick = closeBrowse;
$("#browseModal").onclick = (ev) => { if (ev.target.id === "browseModal") closeBrowse(); };
$("#bUp").onclick = () => BROWSER.parent && browseTo(BROWSER.parent);
$("#bGo").onclick = () => browseTo($("#bPath").value.trim());
$("#bPath").addEventListener("keydown", (e) => { if (e.key === "Enter") browseTo($("#bPath").value.trim()); });
$("#bUseFolder").onclick = useFolder;
$("#bAddVideos").onclick = addSelectedVideos;

/* ---------------------------------------------------------------- model downloads */
let MODELS = { cached: [], expected_mb: {}, download: {} };

async function refreshModels() {
  try {
    MODELS = await api("/api/models");
    const sel = $("#model").value;
    const dl = MODELS.download || {};
    const text = $("#dlText");
    const btn = $("#dlBtn");

    if (dl.status === "downloading") {
      text.textContent = `downloading ${dl.size} · ${dl.pct || 0}% (${dl.mb || 0} MB)`;
      text.className = "";
      btn.classList.add("hidden");
      setTimeout(refreshModels, 2000);
      return;
    }
    if (dl.status === "error" && dl.size === sel) {
      text.textContent = "download failed";
      text.className = "bad";
      btn.textContent = "Retry";
      btn.classList.remove("hidden");
      btn.onclick = () => startModelDownload(sel);
      return;
    }
    const hint = $("#dlHint");
    if (hint) {
      hint.textContent = ["medium", "large-v3"].includes(sel)
        ? "Large models are much slower on CPU (about 0.3–0.5× realtime). Best run overnight."
        : "";
    }
    if ((MODELS.cached || []).includes(sel)) {
      text.textContent = "model ready";
      text.className = "ok";
      btn.classList.add("hidden");
    } else {
      const mb = MODELS.expected_mb?.[sel] || 0;
      text.textContent = `not downloaded · ${mb >= 1000 ? (mb / 1000).toFixed(1) + " GB" : mb + " MB"}`;
      text.className = "";
      btn.textContent = "Download";
      btn.classList.remove("hidden");
      btn.onclick = () => startModelDownload(sel);
    }
  } catch (e) { /* server starting */ }
}

async function startModelDownload(size) {
  try {
    await api("/api/models/download", { method: "POST", body: JSON.stringify({ size }) });
    toast(`Downloading ${size}…`, "ok");
    refreshModels();
  } catch (e) { toast(e.message, "err"); }
}

$("#model").onchange = refreshModels;
refreshModels();
