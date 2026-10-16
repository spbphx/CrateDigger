"""
The main "digging" process: search -> era check -> download -> analysis -> loops -> files.

cut_track() is a pure function (it runs in analysis processes); Session writes a crate to disk;
Pipeline connects the network thread, the analysis processes and the writer thread;
run_dig / run_local / run_rerun are the three jobs the interface can start.
"""
from __future__ import annotations

import json
import queue
import random
import re
import shutil
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np

from collections import Counter

from . import dsp, i18n, iso, workers
from .i18n import P, T, set_lang
from .sources import BotBlocked, Fetcher, Stopped, basic_filter, build_query, era_check, explain_error, human_count

APP_DIR = Path(__file__).resolve().parent.parent

STEREO_DIR = "Stereo_24bit"
MS_DIR = "ModelSamples_mono16"
CHOPS_DIR = "Chops"
SRC_DIR = "Sources_mp3"
KEEP_AS_IS = (".mp3", ".wav", ".flac", ".aif", ".aiff")
PREFER_LABEL = {"any": "any loops", "breaks": "breaks", "melodic": "melody"}
ONLY_PHRASE = {"drums": "drums alone", "bass": "bass alone", "vocal": "voice without a beat", "pads": "pads alone"}


def what_label(p: dict) -> str:
    """What we are looking for — for the log."""
    return T(" or ").join(T(iso.KIND_LABEL[k]) for k in p["only"]) if p["only"] else T(PREFER_LABEL[p["prefer"]])

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def translit(s: str) -> str:
    """Russian letters -> Latin (for file names)."""
    out = []
    for ch in s:
        low = ch.lower()
        if low in _TR:
            t = _TR[low]
            out.append(t.capitalize() if ch.isupper() and t else t)
        else:
            out.append(ch)
    return "".join(out)


def clean_title(title: str, maxlen: int = 20) -> str:
    """A short CamelCase title for file names: no brackets, remaster notes or featuring; ASCII only."""
    t = title or ""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)                       # (Remastered 2015), [Live]
    t = re.sub(r"\s+-\s+.*\b(remaster\w*|version|mix|mono|stereo|edit|live|single)\b.*$", "", t, flags=re.I)
    t = re.sub(r"\b(feat|ft)\.?\s.*$", "", t, flags=re.I)
    t = t.replace("'", "").replace("’", "").replace("`", "")
    t = translit(t)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", t)
    s = "".join((w[:1].upper() + (w[1:].lower() if w.isupper() and len(w) > 3 else w[1:])) for w in words)
    return (s[:maxlen] if maxlen else s) or "Untitled"


def slug(s: str, n=40) -> str:
    """A folder-name-safe slug of the tags ("random" if empty)."""
    s = translit(s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "random"


def safe_name(s: str) -> str:
    """Remove characters that are not allowed in file names."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip(" .")[:120] or "track"


# --------------------------------------------------------------------------- job state
class JobState:
    """Everything the interface polls about the running job: status, progress, log, tracks."""
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.status = "idle"
        self.progress = 0.0
        self.current = ""
        self.log_lines: list[str] = []
        self.tracks: list[dict] = []
        self.dig_name = ""
        self.stop = False
        self.found_cookies = ""
        self.note = ""          # the summary to show instead of "Done"
        self.stop_hooks = []    # what to do at once on "Stop" (kill the analysis processes)
        self.busy = False       # the job thread is still finishing (after "Stop" it no longer blocks a new job)

    def request_stop(self):
        """"Stop": the window is free for a new dig right away. The network drops its request, the analysis
        processes are killed, and the job thread writes its summary into this (now old) JobState."""
        self.stop = True
        with self.lock:
            if self.status == "running":
                self.status = "stopped"
                self.current = ""
        for fn in list(self.stop_hooks):
            try:
                fn()
            except Exception:
                traceback.print_exc()

    def log(self, msg: str):
        """Add a timestamped line to the job log (the interface fetches new lines)."""
        with self.lock:
            self.log_lines.append(time.strftime("%H:%M:%S ") + msg)
            if len(self.log_lines) > 600:
                self.log_lines = self.log_lines[-500:]

    def snapshot(self, since=0):
        """A copy of the state for /api/state; `since` — how many log lines the window already has."""
        with self.lock:
            return dict(status=self.status, progress=round(self.progress, 3), current=self.current,
                        log=self.log_lines[since:], log_total=len(self.log_lines),
                        tracks=self.tracks, dig=self.dig_name, found_cookies=self.found_cookies,
                        note=self.note, busy=self.busy)


# --------------------------------------------------------------------------- history
class History:
    """Video ids already used, so the same record is not dug twice."""
    def __init__(self, root: Path):
        self.path = root / ".crate_history.json"
        try:
            self.seen = set(json.loads(self.path.read_text("utf8")).get("seen", []))
        except Exception:
            self.seen = set()

    def add(self, vid):
        self.seen.add(vid)
        try:
            lst = list(self.seen)[-8000:]
            self.path.write_text(json.dumps({"seen": lst}), "utf8")
        except Exception:
            pass


# --------------------------------------------------------------------------- digging
DEFAULTS = dict(tags="", n_tracks=5, n_loops=3, bars=2, prefer="any", mode="stretch",
                bpm_min=60, bpm_max=120, y0=1960, y1=1999, strict_year=False,
                year_filter=False, bpm_filter=False,
                save_mp3=True, formats="stereo", chops=False, normalize=True, title_len=20,
                cookies_browser="", rare=False, sort_ms=False, only=[])


def _int(v, lo, hi, d):
    try:
        return max(lo, min(hi, int(float(v))))
    except Exception:
        return d


def normalize_params(p: dict) -> dict:
    """Settings from the interface -> a complete, validated dict (unknown or broken values fall back to defaults)."""
    q = dict(DEFAULTS)
    q.update({k: v for k, v in (p or {}).items() if k in DEFAULTS})
    q["n_tracks"] = _int(q["n_tracks"], 1, 20, 5)
    q["n_loops"] = _int(q["n_loops"], 1, 8, 3)
    q["bars"] = q["bars"] if q["bars"] in (1, 2, 4, "1", "2", "4") else 2
    q["bars"] = int(q["bars"])
    q["bpm_min"] = _int(q["bpm_min"], 60, 120, 60) // 5 * 5
    q["bpm_max"] = _int(q["bpm_max"], q["bpm_min"], 120, 120) // 5 * 5
    q["y0"] = _int(q["y0"], 1940, 2025, 1960)
    q["y1"] = _int(q["y1"], q["y0"], 2025, 1999)
    q["title_len"] = _int(q["title_len"], 6, 60, 20)
    # "Only ..." — several at once are allowed (they work as OR); the first row is ignored then
    only = q["only"] or []
    if isinstance(only, str):
        only = only.split(",")
    if q["prefer"] == "drums":          # settings of version 1.0
        only, q["prefer"] = list(only) + ["drums"], "any"
    q["only"] = [k for k in iso.KINDS if k in only]
    q["prefer"] = q["prefer"] if q["prefer"] in PREFER_LABEL else "any"
    q["mode"] = q["mode"] if q["mode"] in ("stretch", "vinyl") else "stretch"
    q["formats"] = q["formats"] if q["formats"] in ("both", "stereo", "mono") else "stereo"
    for k in ("strict_year", "save_mp3", "chops", "normalize", "rare", "sort_ms", "year_filter", "bpm_filter"):
        q[k] = bool(q[k]) and q[k] not in ("0", "false", "False")
    if not q["bpm_filter"]:                            # no filter — the full 60-120 range
        q["bpm_min"], q["bpm_max"] = 60, 120
    return q


def unique_name(base: str, taken: set) -> str:
    """A file name not used yet in this crate (adds _2, _3...)."""
    if base not in taken:
        return base
    for suf in "bcdefghijklmnopqrstuvwxyz":
        if base + suf not in taken:
            return base + suf
    return base + str(random.randint(100, 999))


def _new_dig_dir(root: Path, label: str) -> tuple[Path, str]:
    """Create the crate folder <date>_<tags>[_N]. -> (path, timestamp)"""
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    dig_dir = root / f"{stamp}_{slug(label)}"
    k = 2
    while dig_dir.exists():
        dig_dir = root / f"{stamp}_{slug(label)}_{k}"
        k += 1
    dig_dir.mkdir(parents=True)
    return dig_dir, stamp


def _loop_opts(p):
    """Rendering options shared by all loops of a record."""
    return dict(mode=p["mode"], bpm_min=p["bpm_min"], bpm_max=p["bpm_max"],
                fixed_bpm=None, max_stretch=0.05, normalize=p["normalize"])


def _max_stretch(p: dict, kind: str | None) -> float:
    """How much the tempo may be fitted. Vocals in "stretching" — as much as needed (pitch doesn't
    change), in "pitching" — up to +-15 %; everything else — up to +-5 %."""
    if kind == "vocal":
        return 0.15 if p["mode"] == "vinyl" else 1.0
    return 0.05


VOCAL_EDGE_DB = 6.0      # vocal loops whose start and end differ in loudness by more go to the end of the queue


def _queue(an, kind: str, cands: list[dict], rng) -> list[dict]:
    """Candidates of one "Only" type in the order they should be used:
    "clean" before "with extras", then by rank (with a little randomness, as in real digging).
    Spans with a predictable seam problem go last and are used only if nothing else is left:
      * vocals — loudness at the start and at the end differ a lot (the loop jumps at the restart);
      * off-grid pads — something new comes in during the last half second (it would be cut off).
    Also attaches purity (0..1) and the detector metrics for Debugging mode."""
    for c in cands:
        c["_r"] = c["rank"] + float(rng.normal(0, 0.012))
        late = False
        if kind == "drums":
            c["purity"] = an.drum_purity(c["t0"], c["t1"])
            c["dbg"] = {k: round(float(v), 3) for k, v in an.drum_metrics(c["t0"], c["t1"]).items()}
        else:
            m = an.iso_metrics(c["t0"], c["t1"])
            c["purity"] = float(np.clip(iso.score(m, kind), 0, 1))
            c["dbg"] = {k: round(float(v), 2) for k, v in m.items()}
            if kind == "vocal":
                jump = an.edge_jump(c["t0"], c["t1"])
                c["dbg"]["edge_jump"] = round(jump, 1)
                late = abs(jump) > VOCAL_EDGE_DB
            elif kind == "pads" and c.get("free"):
                late = an.late_entry(c["t0"], c["t1"])
                c["dbg"]["late_entry"] = bool(late)
        c["late"] = late
    return sorted(cands, key=lambda c: (c["late"], -c.get("grade", 2), -c["_r"]))


def cut_track(path, p: dict, hint_bpm: int | None = None, lenient: bool = False, tmp_dir=None) -> dict:
    """Analysis of one track and cutting of loops WITHOUT writing anything to disk — a pure function
    (the pipeline processes run it). hint_bpm — the most common BPM in the crate (beatless pads are cut
    to it). lenient — take as many loops as were found. tmp_dir — where ffmpeg temporarily puts the
    decoded audio (then it doesn't take the process's memory).
    -> dict(loops=[...with audio...], logs=[...], reason=None | "why the track didn't fit", tempo, tuning)"""
    set_lang(p.get("lang"))              # the analysis process writes its reasons in the interface language
    x = dsp.load_audio_i16(path, tmp_dir=tmp_dir)
    try:
        return _cut(x, p, hint_bpm, lenient)
    finally:
        dsp.release_audio(x)


def _cut(x, p: dict, hint_bpm, lenient) -> dict:
    """The body of cut_track(): analysis, candidate selection, rendering. x — int16 audio."""
    t_start = time.time()
    an = dsp.TrackAnalysis(x, want_iso=any(k in iso.ISO_KINDS for k in p["only"]))
    res = dict(loops=[], reason=None, tempo=round(an.tempo, 2), tuning=round(an.tuning, 1),
               logs=[T("   tempo ≈{tempo:.1f} BPM, tuning {tuning:+.0f} cents, analysis {sec:.1f} s",
                       tempo=an.tempo, tuning=an.tuning, sec=time.time() - t_start)])
    only, n = p["only"], p["n_loops"]
    free_ok = any(k in ("pads", "vocal") for k in only)   # a grid and an exact tempo aren't required for these
    why = an.rhythm_problem()
    if why and not free_ok:
        res["reason"] = T("{why} — won't loop cleanly", why=why)
        return res
    track_fit = dsp.choose_target(an.tempo, p["bpm_min"], p["bpm_max"], None, 0.05)
    if track_fit is None and not free_ok:
        res["reason"] = T("tempo {tempo:.1f} is outside the {a}–{b} range", tempo=an.tempo, a=p["bpm_min"], b=p["bpm_max"])
        return res

    rng = np.random.default_rng()
    if only:
        free_bpm = None
        if free_ok:
            if not why and track_fit:
                free_bpm = track_fit[0]
            elif hint_bpm and p["bpm_min"] <= hint_bpm <= p["bpm_max"]:
                free_bpm = int(hint_bpm)
            else:
                free_bpm = 5 * round((p["bpm_min"] + p["bpm_max"]) / 10)
        queues = {k: _queue(an, k, an.iso_candidates(k, p["bars"], free_bpm), rng) for k in only}
        dry = dsp.interleave_loops(queues, n, accept=lambda c: c)      # first guess of the picks (no rendering)
    else:
        free_bpm = None
        dry = an.pick_loops(n + 4, p["bars"], p["prefer"], rng)

    # one target tempo for the whole record — loops of one track will fit each other
    opts, fixed = _loop_opts(p), None
    grid = [c for c in dry[:n] if not c.get("free")]
    if grid:
        bt = an.beat_times
        reals = [60.0 * (c["e"] - c["s"]) / (bt[c["e"]] - bt[c["s"]]) for c in grid]
        fixed = dsp.choose_target(float(np.median(reals)), p["bpm_min"], p["bpm_max"], None, 0.05)
    free_target = fixed[0] if fixed else free_bpm

    def render(c):
        """Render one candidate -> loop dict with audio (or None if it can't be fitted / has a bad seam)."""
        kind = c.get("kind")
        if c.get("free"):
            r = dsp.render_free(an, c["t0"], p["bars"], free_target, dict(opts, kind=kind))
        else:
            ms = _max_stretch(p, kind)
            o = dict(opts, kind=kind, max_stretch=ms)
            if fixed:
                o.update(fixed_bpm=fixed[0], max_stretch=max(0.07, ms))
            r = dsp.render_loop(an, c["s"], c["e"], o)
            if r is None and kind == "pads" and free_target:      # the pad didn't fit the tempo — cut it without stretching
                r = dsp.render_free(an, c["t0"], p["bars"], free_target, dict(opts, kind=kind))
        if r:
            r["score"] = round(float(c.get("rank", c.get("score", 0))), 3)
            if kind:
                r.update(kind=kind, mixed=c.get("grade", 2) < 2, grade=int(c.get("grade", 2)),
                         purity=round(float(c.get("purity", 0)), 2), dbg=c.get("dbg", {}))
        return r

    if only:          # several "Only" types take turns; inside a type — cleanest first
        rendered = dsp.interleave_loops(queues, n, accept=render)
    else:
        rendered = []
        for c in dry:
            if len(rendered) >= n:
                break
            r = render(c)
            if r:
                rendered.append(r)

    need = 1 if only else n               # isolated parts are rare — one loop is enough
    if len(rendered) < need and not lenient:
        res["reason"] = (T("no parts with {what} — taking another record",
                           what=T(" or ").join(T(ONLY_PHRASE[k]) for k in only))
                         if only else T("found only {k} good loop(s) of {n}", k=len(rendered), n=n))
        return res
    if not rendered:
        res["reason"] = T("no clean loop found")
        return res
    if not only:
        rendered.sort(key=lambda r: r["src_start"])      # "Any/Breaks/Melody": in track order
    res["loops"] = rendered
    return res


class Session:
    """One "crate": a folder, metadata, unique names."""

    def __init__(self, root: Path, state: JobState, p: dict, label: str):
        self.root, self.state, self.p = root, state, p
        self.dig_dir, stamp = _new_dig_dir(root, label)
        self.meta = dict(created=stamp, params=p, tracks=[])
        self.taken: set[str] = set()
        self.lock = threading.Lock()
        self.log = state.log
        with state.lock:
            state.dig_name = self.dig_dir.name
            state.tracks = []
        self.log(T("📦 New crate: {name}", name=self.dig_dir.name))
        self.log(T("🎚 Tempo fit: pitching (tempo and pitch change together)") if p["mode"] == "vinyl"
                 else T("🎚 Tempo fit: stretching (hits stay in place, same pitch)"))

    def common_bpm(self) -> int | None:
        """The most common BPM among the loops of this crate."""
        with self.lock:
            c = Counter(l["bpm"] for t in self.meta["tracks"] for l in t["loops"])
        return c.most_common(1)[0][0] if c else None

    def save(self, path: Path, track: dict, res: dict):
        """Write loops to disk and to dig.json (only in the main process — unique names are decided here)."""
        p, log = self.p, self.log
        label = f"{track['artist']} — {track['title']}"
        short = clean_title(track["title"], p["title_len"])
        track.update(tempo=res["tempo"], tuning=res["tuning"], loops=[])
        dig_dir = self.dig_dir
        for i, r in enumerate(res["loops"], 1):
            letter = iso.LETTER.get(r.get("kind"), "")          # Amin95BQuark1 — the type letter goes after the key
            name = unique_name(f"{r['key']}{r['bpm']}{letter}{short}{i}", self.taken)
            self.taken.add(name)
            files = {}
            if p["formats"] in ("both", "stereo"):
                d = dig_dir / STEREO_DIR
                d.mkdir(exist_ok=True)
                dsp.write_wav(d / f"{name}.wav", r["audio"], bits=24)
                files["stereo"] = f"{STEREO_DIR}/{name}.wav"
            if p["formats"] in ("both", "mono"):
                rel = f"{MS_DIR}/{r['key']}/{r['bpm']:03d} BPM" if p["sort_ms"] else MS_DIR
                d = dig_dir / rel
                d.mkdir(parents=True, exist_ok=True)
                dsp.write_wav(d / f"{name}.wav", r["audio"].mean(1), bits=16)
                files["mono"] = f"{rel}/{name}.wav"
            if p["chops"]:
                d = dig_dir / CHOPS_DIR / name
                d.mkdir(parents=True, exist_ok=True)
                a = r["audio"]
                L = len(a) // 8
                for j in range(8):
                    piece = a[j * L:(j + 1) * L].copy()
                    fo = min(len(piece) // 4, int(0.006 * dsp.SR))
                    piece[-fo:] *= np.linspace(1, 0, fo)[:, None]
                    dsp.write_wav(d / f"{name}_{j + 1}.wav", piece.mean(1), bits=16)
            loop = {k: v for k, v in r.items() if k != "audio"}
            loop.update(name=name, files=files, n=i, steps=int(round(r["bars"] * 16)),
                        peaks=dsp.waveform_peaks(r["audio"]))
            track["loops"].append(loop)
        if p["save_mp3"]:
            self._keep_source(path, track, label)
        with self.lock:
            self.meta["tracks"].append(track)
        with self.state.lock:
            self.state.tracks = list(self.meta["tracks"])
        write_meta(dig_dir, self.meta)
        log(f"✅ {label}: " + ", ".join(l["name"] for l in track["loops"]))

    def _keep_source(self, path: Path, track: dict, label: str):
        """The source track next to the loops — the record can be re-run from it without YouTube."""
        d = self.dig_dir / SRC_DIR
        d.mkdir(exist_ok=True)
        base = safe_name(label.replace(" — ", " - "))
        try:
            if path.suffix.lower() in KEEP_AS_IS:          # mp3/wav/flac are copied without re-encoding
                dst = d / (base + path.suffix.lower())
                shutil.copy2(path, dst)
            else:
                dst = d / (base + ".mp3")
                dsp.save_mp3(path, dst)
            track["src"] = f"{SRC_DIR}/{dst.name}"
        except Exception as e:
            self.log(T("   (source not kept: {e})", e=str(e)[:120]))

    def finish(self, n_records):
        """Final save; an empty crate folder is removed. -> crate folder or None"""
        write_meta(self.dig_dir, self.meta)
        total = sum(len(t["loops"]) for t in self.meta["tracks"])
        if total == 0:
            shutil.rmtree(self.dig_dir, ignore_errors=True)
            with self.state.lock:
                self.state.dig_name = ""
        self.log(T("🏁 Done: {n} {rw}, {total} {lw}", n=n_records, rw=P(n_records, "record"),
                   total=total, lw=P(total, "loop")) + (f" → {self.dig_dir}" if total else ""))
        return self.dig_dir if total else None


class Pipeline:
    """The pipeline: tracks are analysed in separate processes (workers.Analyzer), and the results are
    written only by this process — in one writer thread, one at a time (no races for names and
    dig.json). Meanwhile the network downloads the next track."""

    def __init__(self, sess: "Session", state: JobState, total: int, lenient: bool = False, on_accept=None):
        self.sess, self.state, self.p = sess, state, sess.p
        self.total, self.lenient, self.on_accept = total, lenient, on_accept
        self.log = state.log
        self.pool = workers.Analyzer(self.log, tasks=total, tmp=sess.root / ".tmp" / "analysis")
        if self.pool.parallel and self.pool.n > 1:
            self.log(T("⚙ Analysing in {n} parallel processes", n=self.pool.n))
        self.cv = threading.Condition()
        self.pending: dict[int, str] = {}         # sent to analysis, not written yet: number -> title
        self.accepted = 0
        self.net = ""                             # what the network is doing right now (for the status line)
        self._n = 0
        self.q: queue.Queue = queue.Queue()
        self.saver = threading.Thread(target=self._save_loop, daemon=True, name="saver")
        self.saver.start()
        state.stop_hooks.append(self.pool.kill)  # "Stop" — at once, even if the network is busy downloading

    @property
    def slots(self) -> int:
        """How many tracks to keep in analysis at the same time."""
        return self.pool.n if self.pool.parallel else 1

    def counts(self) -> tuple[int, int]:
        with self.cv:
            return self.accepted, len(self.pending)

    def submit(self, path, track: dict, cleanup: bool = True):
        """Hand a downloaded track to analysis. cleanup — delete the file when it is no longer needed."""
        label = f"{track['artist']} — {track['title']}"
        with self.cv:
            self._n += 1
            key = self._n
            self.pending[key] = label
        self.status()
        p = dict(self.p, lang=i18n.LANG)              # language of messages — the current interface language
        fut = self.pool.submit(cut_track, str(path), p, self.sess.common_bpm(), self.lenient, self.pool.tmp)
        fut.add_done_callback(lambda f: self.q.put((key, f, Path(path), track, label, cleanup)))

    def _save_loop(self):
        """Writer thread: takes finished analyses from the queue and saves them one at a time."""
        while True:
            item = self.q.get()
            if item is None:
                return
            key, fut, path, track, label, cleanup = item
            try:
                self._handle(fut, path, track, label)
            except Exception as e:
                self.log(T("✕ {label}: not saved ({e})", label=label, e=str(e)[:200]))
                traceback.print_exc()
            finally:
                if cleanup:
                    path.unlink(missing_ok=True)
                with self.cv:
                    self.pending.pop(key, None)
                    self.cv.notify_all()
                self.status()

    def _handle(self, fut, path: Path, track: dict, label: str):
        """Log the result of one analysis and save its loops if the track fits."""
        if self.state.stop or fut.cancelled():
            return
        try:
            res = fut.result()
        except workers.BrokenProcessPool:
            self.log(T("✕ {label}: the analysis process crashed (maybe out of memory) — skipping", label=label))
            return
        except Exception as e:
            self.log(f"✕ {label}: {str(e)[:300]}")
            return
        for line in res["logs"]:
            self.log(f"   {line.strip()} — {track['title'][:40]}")
        if res["reason"]:
            self.log(f"⏭ {label}: {res['reason']}")
            return
        with self.cv:
            if self.accepted >= self.total and not self.lenient:
                return                            # already have enough (just in case)
        self.sess.save(path, track, res)
        with self.cv:
            self.accepted += 1
            self.cv.notify_all()
        if self.on_accept:
            self.on_accept(track)

    def wait(self, timeout: float = 0.5):
        with self.cv:
            self.cv.wait(timeout)

    def status(self, net: str | None = None):
        """Update the status line and the progress bar (net — what the network is doing)."""
        if net is not None:
            self.net = net
        with self.cv:
            names = [n.split(" — ", 1)[-1][:28] for n in self.pending.values()]
            acc, busy = self.accepted, len(self.pending)
        parts = [self.net] if self.net else []
        if names:
            parts.append(T("analysing: {names}", names=", ".join(names)))
        with self.state.lock:
            self.state.current = " · ".join(parts) or T("Working…")
            self.state.progress = min(0.999, (acc + 0.5 * busy) / max(1, self.total))

    def finish(self):
        """Wait for the started analyses (on "Stop" — abandon them) and close the processes."""
        self.status(net="")
        deadline = None
        while True:
            if self.state.stop:
                self.pool.kill()
                deadline = deadline or time.time() + 5     # let the killed tasks report back
            with self.cv:
                if not self.pending or (deadline and time.time() > deadline):
                    break
                self.cv.wait(0.3)
        self.q.put(None)
        self.saver.join(timeout=10)
        if self.pool.kill in self.state.stop_hooks:
            self.state.stop_hooks.remove(self.pool.kill)
        if not self.state.stop:
            self.pool.close()


def run_dig(params: dict, root: Path, state: JobState):
    """Dig on YouTube Music: the network strictly one request at a time in this thread, analysis in processes."""
    p = normalize_params(params)
    sess = Session(root, state, p, p["tags"])
    log = state.log
    log(T("🎛 Tags: {tags}; {years}; {bpm}; {n} records × {k} loops, {bars} bar(s)",
          tags=p["tags"] or T("none, random records"),
          years=T("years {a}–{b}", a=p["y0"], b=p["y1"]) if p["year_filter"] else T("any years"),
          bpm=f"BPM {p['bpm_min']}–{p['bpm_max']}" if p["bpm_filter"] else T("any BPM"),
          n=p["n_tracks"], k=p["n_loops"], bars=p["bars"]))
    hist = History(root)
    rng = random.Random()
    try:
        fetcher = Fetcher(root / ".tmp", log, p["cookies_browser"], cookie_file=APP_DIR / "cookies.txt",
                          stop=lambda: state.stop)
    except ImportError:
        raise RuntimeError(T("yt-dlp is not installed. Launch the program with the start file — it "
                             "installs everything."))

    n = p["n_tracks"]
    pool: list[dict] = []
    tried: set[str] = set()
    attempts = queries = empty_streak = 0
    max_attempts, max_queries = n * 12, n * 10
    if p["only"]:                         # isolated parts are rare — dig deeper
        max_attempts, max_queries = n * 25, n * 20
    if p["rare"]:
        max_queries += n * 6
    rare_limit, popular_streak = 150_000, 0
    if p["rare"]:
        log(T("💎 Looking for rare ones: records with the fewest plays first (up to 150K)"))
    if p["only"]:
        log(T("🎯 Looking for: {what}. Clean parts first, then parts with extras; such parts are rare, "
              "so digging takes longer",
              what=what_label(p)))
    pipe = Pipeline(sess, state, n, on_accept=lambda t: hist.add(t["id"]))
    net_stopped = False                   # 429 or a bot check: network stops, downloaded tracks are finished

    try:
        while True:
            if state.stop:
                log(T("⏹ Stopped"))
                break
            acc, busy = pipe.counts()
            if acc >= n:
                break
            # no running ahead: don't download more than may be needed, nor more than can be analysed
            if net_stopped or acc + busy >= n or busy >= pipe.slots:
                if net_stopped and not busy:
                    break
                pipe.status(net="")
                pipe.wait(0.5)
                continue
            if attempts >= max_attempts:
                log(T("😕 Went through too many records — try other tags or loosen the filters."))
                break
            if not pool:
                if queries >= max_queries or empty_streak >= 6:
                    log(T("😕 The search finds nothing more. Try other tags."))
                    break
                yr = (p["y0"], p["y1"]) if p["year_filter"] else (None, None)
                q = build_query(p["tags"], *yr, rng, only=p["only"], rare=p["rare"])
                queries += 1
                pipe.status(net=T("Searching: “{q}”", q=q))
                res = fetcher.search(q)
                fresh = [r for r in res if r["id"] not in hist.seen and r["id"] not in tried and not basic_filter(r)]
                log(T("🔎 “{q}”: found {n}, new suitable {k}", q=q, n=len(res), k=len(fresh)))
                if not fresh:
                    empty_streak += 1
                    continue
                empty_streak = 0
                rng.shuffle(fresh)            # dig deep into the results, not only the first lines
                if p["rare"]:
                    # least played first; without a counter — the far lines of the results
                    known = sorted([r for r in fresh if r.get("views") is not None], key=lambda r: r["views"])
                    unknown = [r for r in fresh if r.get("views") is None]
                    fresh = known[:4] + unknown[:2] if known else unknown
                pool.extend(fresh[:3])
                continue

            cand = pool.pop(0)
            tried.add(cand["id"])
            attempts += 1
            label = f"{cand.get('artist') or '?'} — {cand.get('title') or '?'}"
            path = None
            try:
                pipe.status(net=T("Checking: {label}", label=label))
                info = fetcher.probe(cand)
                title = info.get("track") or cand.get("title") or info.get("title") or "Untitled"
                artist = (info.get("artist") or cand.get("artist") or info.get("creator") or
                          (info.get("uploader") or "").replace(" - Topic", "") or "Unknown")
                if len(artist) > 60:
                    artist = artist.split(",")[0].strip()
                dur = info.get("duration") or cand.get("duration") or 0
                label = f"{artist} — {title}"
                if dur and (dur < 95 or dur > 600):
                    log(T("⏭ {label}: length {sec} s — skipping", label=label, sec=int(dur)))
                    continue
                views = info.get("view_count")
                if views is None:
                    views = cand.get("views")
                if p["rare"] and views is not None and views > rare_limit:
                    log(T("⏭ {label}: too well known ({views} plays)", label=label, views=human_count(views)))
                    popular_streak += 1
                    if popular_streak >= 6 and rare_limit < 3_000_000:
                        rare_limit *= 2
                        popular_streak = 0
                        log(T("   (nothing rare here — raising the limit to {n})", n=human_count(rare_limit)))
                    continue
                popular_streak = 0
                if p["year_filter"]:
                    ok, year, why = era_check(cand, info, p["y0"], p["y1"], p["strict_year"])
                else:                                   # years aren't filtered — the year is only for the caption
                    ok, year, why = True, era_check(cand, info, 1900, 2100, False)[1], ""
                if not ok:
                    log(T("⏭ {label}: wrong era ({why})", label=label, why=why))
                    continue
                pipe.status(net=T("Downloading: {label}", label=label))
                extra = [str(year)] if year else []
                if views is not None:
                    extra.append(T("{views} plays", views=human_count(views)))
                log(f"⬇ {label}" + (f" ({', '.join(extra)})" if extra else ""))
                path = fetcher.download(cand, info)
                if state.stop:
                    continue
                track = dict(id=cand["id"], title=title, artist=artist,
                             album=info.get("album") or cand.get("album") or "", year=year,
                             url=f"https://music.youtube.com/watch?v={cand['id']}", query=cand.get("query", ""),
                             thumb=info.get("thumbnail") or "", views=views)
                pipe.submit(path, track)                # the pipeline deletes the file after analysis
                path = None
            except Stopped:                             # "Stop" in the middle of a request — don't wait for the answer
                log(T("⏹ Stopped"))
                break
            except BotBlocked as e:
                log(f"⛔ {e}")
                log(T("   Network stopped so as not to burn through records; finishing what's already "
                      "downloaded."))
                net_stopped = True
            except Exception as e:
                msg = explain_error(e)
                log(f"✕ {label}: {msg}")
                if "429" in msg:
                    log(T("   Network stopped so YouTube doesn't restrict more; finishing what's already "
                          "downloaded."))
                    net_stopped = True
                if not isinstance(e, RuntimeError):
                    traceback.print_exc()
            finally:
                if path:
                    Path(path).unlink(missing_ok=True)
                if fetcher.found_browser:
                    with state.lock:
                        state.found_cookies = fetcher.found_browser
    except Stopped:                         # "Stop" during a search — don't wait for the answer
        log(T("⏹ Stopped"))
    pipe.finish()
    return sess.finish(pipe.accepted)


def run_local(files: list[tuple[Path, str]], params: dict, root: Path, state: JobState):
    """Cut your own files (vinyl rips etc.) — in parallel. files = [(path, original name)]"""
    p = normalize_params(params)
    sess = Session(root, state, p, "my-files")
    pipe = Pipeline(sess, state, len(files), lenient=True)
    for i, (path, orig) in enumerate(files):
        if state.stop:
            Path(path).unlink(missing_ok=True)
            continue
        stem = Path(orig).stem
        artist, title = (stem.split(" - ", 1) if " - " in stem else ("", stem))
        track = dict(id=f"local-{i}-{slug(stem, 30)}", title=title.strip() or stem, artist=artist.strip() or T("My "
                                                                                                               "file"),
                     album="", year=None, url="", query="", thumb="", local=True)
        pipe.submit(path, track)
    pipe.finish()
    if state.stop:
        state.log(T("⏹ Stopped"))
    return sess.finish(pipe.accepted)


def find_source(dig_dir: Path, track: dict) -> Path | None:
    """The source track in the crate folder (old crates have no path in dig.json — search by name)."""
    cands = [dig_dir / track["src"]] if track.get("src") else []
    cands.append(dig_dir / SRC_DIR / (safe_name(f"{track.get('artist')} - {track.get('title')}") + ".mp3"))
    root = dig_dir.resolve()
    for c in cands:
        c = c.resolve()
        if root in c.parents and c.is_file():
            return c
    return None


def run_rerun(dig_dir: Path, old: dict, params: dict, root: Path, state: JobState):
    """Re-run an already found record with new settings (pitching/stretching, loop type and length...)."""
    p = normalize_params(params)
    track = {k: old.get(k) for k in ("id", "title", "artist", "album", "year", "url", "query", "thumb", "views")}
    if old.get("local"):
        track["local"] = True
    label = f"{track['artist']} — {track['title']}"
    sess = Session(root, state, p, "rerun " + (track["title"] or "track"))
    log = state.log
    log(T("🔁 Re-running: {label} — {what}, {bars} bar(s), up to {n} loops",
          label=label, what=what_label(p), bars=p["bars"], n=p["n_loops"]))
    pipe = Pipeline(sess, state, 1, lenient=True)
    try:
        src, downloaded = find_source(dig_dir, old), False
        if src is None:
            if old.get("local") or not old.get("url"):
                raise RuntimeError(T("the source file was not kept — drag it into the window again"))
            log(T("   the source isn't in the crate folder — downloading again"))
            pipe.status(net=T("Downloading: {label}", label=label))
            fetcher = Fetcher(root / ".tmp", log, p["cookies_browser"], cookie_file=APP_DIR / "cookies.txt",
                              stop=lambda: state.stop)
            src, downloaded = fetcher.download({"id": old["id"]}), True
            if fetcher.found_browser:
                with state.lock:
                    state.found_cookies = fetcher.found_browser
        if not state.stop:
            pipe.submit(src, track, cleanup=downloaded)   # the source in the crate is not deleted
    except Stopped:
        log(T("⏹ Stopped"))
    except Exception as e:
        log(f"✕ {label}: {explain_error(e)}")
    pipe.finish()
    ok = pipe.accepted > 0
    if not ok and not state.stop:
        with state.lock:
            state.note = T("No loops found with these settings — details in the log")
    return sess.finish(int(ok))


def write_meta(dig_dir: Path, meta: dict):
    """Write dig.json, credits.txt and MODEL_SAMPLES.txt of the crate."""
    (dig_dir / "dig.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf8")
    lines = [T("CrateDigger — where the loops come from (useful for sample clearance)"), ""]
    for t in meta["tracks"]:
        lines.append(f"{t['artist']} — {t['title']}" + (f" ({t['year']})" if t.get("year") else ""))
        lines.append(f"  {t['url']}")
        for l in t["loops"]:
            a, b = l["src_start"], l["src_end"]
            how = (T("original tempo {bpm}, stretch {pct:+.1f}%", bpm=l["real_bpm"], pct=l["stretch_pct"])
                   if l.get("real_bpm") else T("no grid, cut exactly to tempo"))
            kind = (f", {T(iso.KIND_LABEL[l['kind']])}{T(" (with extras)") if l.get('mixed') else ''}"
                    if l.get("kind") else "")
            lines.append(f"  {l['name']}: {int(a // 60)}:{a % 60:05.2f}–{int(b // 60)}:{b % 60:05.2f}, "
                         + T("{bars} bar(s)", bars=f"{l['bars']:g}") + f", {how}{kind}")
        lines.append("")
    (dig_dir / "credits.txt").write_text("\n".join(lines), "utf8")
    params = meta.get("params") or {}
    mono = params.get("formats") in ("both", "mono")
    ms = [T("How to play these loops on the Elektron Model:Samples"), "",
          (T("1. Transfer the files from the ModelSamples_mono16 folder with Elektron Transfer.") if mono else
           T("1. Transfer the files from the Stereo_24bit folder with Elektron Transfer (it converts "
             "to mono itself).")),
          T("2. Pattern tempo = the number in the file name (Gmaj90Name1 -> 90 BPM)."),
          T("3. Track Length = the number of steps from the table below, trig on step 1."),
          T("   The loop restarts every cycle, so it never drifts."),
          T("4. Chops: the loops are exactly bar-length, so the START knob splits them evenly:"),
          T("   0, 16, 32, 48, 64, 80, 96, 112 ≈ eighths of the loop. Put different START values on"),
          T("   different steps (parameter locks) — you get re-chopping, like on an MPC."),
          T("5. Loops with the same or a neighbouring Camelot number (8A, 8B, 7A, 9A) sound good together."),
          T("6. If there are more than 64 steps, set the track SCALE to 1/2X and halve the length."),
          *([T("7. The files are sorted into folders: ModelSamples_mono16/<key>/<BPM>/ —"),
             T("   transfer the whole folder and find loops on the device by key and tempo.")]
            if mono and params.get("sort_ms") else []), "",
          f"{T("File"):<34}{'BPM':>5}{T("Bars"):>9}{T("Steps"):>7}  {T("Key"):<8} Camelot"]
    for t in meta["tracks"]:
        for l in t["loops"]:
            ms.append(f"{l['name']:<34}{l['bpm']:>5}{l['bars']:>9g}{l['steps']:>7}  {l['key']:<8} {l['camelot']}")
    (dig_dir / "MODEL_SAMPLES.txt").write_text("\n".join(ms), "utf8")
