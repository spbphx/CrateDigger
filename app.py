"""
CrateDigger — sample digging for boom bap.
Starts a local web server and opens the interface in a browser window.

The server is a small JSON API over http.server (no frameworks): the page ui.html polls
/api/state while a job runs; jobs (dig, own files, re-run) run in a background thread.
"""
from __future__ import annotations

import filecmp
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import re
import time
import traceback
import uuid
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from digger import core, dsp, i18n  # noqa: E402
from digger.i18n import T  # noqa: E402

SETTINGS_PATH = APP_DIR / "settings.json"
FAV_DIR = "Favorites"          # copies of favourite loops on disk
FEEDBACK_DIR = "Feedback"      # Debugging mode: loops marked "wrong match" with the analyzer's readings
FAV_KEY = "★favorites"         # the Favorites "crate" on screen: collected from all crates
VERSION = "1.3.2"


def default_out_dir() -> Path:
    """Music/CrateDigger in the home folder (or ~/CrateDigger if there is no Music folder)."""
    home = Path.home()
    music = home / "Music"
    return (music if music.exists() else home) / "CrateDigger"


def load_settings() -> dict:
    """settings.json next to the program, with migrations from older versions."""
    s = {"out_dir": str(default_out_dir()), "last": {}}
    try:
        s.update(json.loads(SETTINGS_PATH.read_text("utf8")))
    except Exception:
        pass
    if s.get("v", 0) < 12:                     # 1.2: the default is now "Stereo 24-bit only"
        if s["last"].get("formats") == "both":
            s["last"]["formats"] = "stereo"
        s["v"] = 12
    if s.get("lang") not in i18n.LANGS:      # language not chosen yet — the window will take the browser's language
        s.pop("lang", None)
    return s


def save_settings(s: dict):
    """Write settings.json (errors are ignored — settings are a convenience)."""
    try:
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf8")
    except Exception:
        pass


SETTINGS = load_settings()
i18n.set_lang(SETTINGS.get("lang") or "ru")
STATE = core.JobState()
WORKER: threading.Thread | None = None
# what is on screen: crates of this working session (while the program runs) and hidden loops
SESSION = {"digs": [], "hidden": set()}
SESSION_LOCK = threading.Lock()


def session_add(dig: str):
    """Remember that a crate is shown on screen in this session."""
    if not dig:
        return
    with SESSION_LOCK:
        if dig not in SESSION["digs"]:
            SESSION["digs"].append(dig)


def out_root() -> Path:
    """The folder where crates are created (created if missing)."""
    p = Path(SETTINGS.get("out_dir") or default_out_dir()).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def open_in_os(path: Path):
    """Open a folder in Explorer / Finder / the file manager."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def health() -> dict:
    """Versions of the tools the program needs (shown at the bottom of the left panel)."""
    h = {"version": VERSION, "python": sys.version.split()[0]}
    try:
        import yt_dlp
        h["yt_dlp"] = yt_dlp.version.__version__
    except Exception:
        h["yt_dlp"] = None
    try:
        import ytmusicapi  # noqa: F401
        h["ytmusicapi"] = True
    except Exception:
        h["ytmusicapi"] = False
    try:
        h["ffmpeg"] = bool(dsp.ffmpeg_exe())
    except Exception:
        h["ffmpeg"] = False
    from digger.sources import js_runtimes
    js = js_runtimes()
    h["js"] = list(js.keys())[0] if js else None
    h["out_dir"] = str(out_root())
    return h


def all_digs():
    """(crate name, dig.json) — newest first."""
    root = out_root()
    for d in sorted([p for p in root.iterdir() if p.is_dir() and (p / "dig.json").exists()],
                    key=lambda p: p.name, reverse=True):
        try:
            yield d.name, json.loads((d / "dig.json").read_text("utf8"))
        except Exception:
            continue


def list_digs():
    """Past crates for the "Add a past crate" list: name and number of loops."""
    out = []
    for name, m in all_digs():
        if len(out) >= 80:
            break
        n = sum(len(t["loops"]) for t in m.get("tracks", []))
        out.append(dict(name=name, loops=n, records=len(m.get("tracks", []))))
    return out


def favorites() -> list[dict]:
    """Everything ever starred: records with only their favourite loops.
    Every record has a `dig` field — the real crate where its files are."""
    out = []
    for name, m in all_digs():
        for t in m.get("tracks", []):
            loops = [l for l in t.get("loops", []) if l.get("fav")]
            if loops:
                out.append(dict(t, loops=loops, dig=name))
    return out


def set_favorite(dig: str, name: str, on: bool) -> bool | None:
    """The star: copy the loop into Favorites or remove the mark (the copy is deleted). None — loop not found."""
    l = get_loop(dig, name)
    if not l:
        return None
    fav_root = out_root() / FAV_DIR
    if on:
        saved = []
        for rel in (l.get("files") or {}).values():
            src = safe_path(f"{dig}/{rel}")
            if not (src and src.exists()):
                continue
            dst, k = fav_root / rel, 2
            while dst.exists() and not filecmp.cmp(dst, src, shallow=False):   # another loop with the same name
                dst = dst.with_name(f"{Path(rel).stem}_{k}{dst.suffix}")
                k += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            saved.append(dst.relative_to(fav_root).as_posix())
        if not saved:
            return None
        update_loop_meta(dig, name, fav=True, fav_files=saved)
        return True
    root = fav_root.resolve()
    for rel in l.get("fav_files") or list((l.get("files") or {}).values()):   # older ones — by the previous scheme
        f = (fav_root / rel).resolve()
        if root not in f.parents:
            continue
        f.unlink(missing_ok=True)
        for d in f.parents:                        # empty folders inside Favorites are removed too
            if d == root:
                break
            try:
                d.rmdir()
            except OSError:
                break
    update_loop_meta(dig, name, fav=False, fav_files=[])
    return False


def set_wrong(dig: str, name: str, on: bool) -> bool | None:
    """Debugging mode, the 👎 "wrong match" button. On: copy the loop's WAV into Feedback/ and append
    one JSON line to Feedback/feedback.jsonl with everything the analyzer knew about the loop (type,
    grade, purity, detector metrics, position in the source, settings). Off: remove both again.
    The folder can simply be zipped and sent to whoever tunes the detectors. None — loop not found."""
    m = read_dig(dig)
    track = next((t for t in m.get("tracks", []) for l in t.get("loops", []) if l.get("name") == name), None)
    l = get_loop(dig, name)
    if not l or track is None:
        return None
    root = out_root() / FEEDBACK_DIR
    log = root / "feedback.jsonl"
    old = []
    if log.exists():                                  # drop an earlier record of this loop (re-mark = refresh)
        old = [x for x in log.read_text("utf8").splitlines() if x.strip()]
        keep = []
        for x in old:
            try:
                rec = json.loads(x)
            except Exception:
                keep.append(x)
                continue
            if rec.get("dig") == dig and rec.get("name") == name:
                if rec.get("wav"):
                    (root / rec["wav"]).unlink(missing_ok=True)
            else:
                keep.append(x)
        old = keep
    if on:
        files = l.get("files") or {}
        src = next((safe_path(f"{dig}/{files[k]}") for k in ("stereo", "mono") if files.get(k)), None)
        wav = ""
        if src and src.exists():
            root.mkdir(parents=True, exist_ok=True)
            dst, k = root / f"{name}.wav", 2
            while dst.exists():
                dst, k = root / f"{name}_{k}.wav", k + 1
            shutil.copy2(src, dst)
            wav = dst.name
        rec = dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), version=VERSION, dig=dig, name=name, wav=wav,
                   artist=track.get("artist"), title=track.get("title"), url=track.get("url"),
                   **{k: l.get(k) for k in ("kind", "mixed", "grade", "purity", "seam", "src_start", "src_end",
                                             "bpm", "real_bpm", "engine", "stretch_pct", "pitch_st", "bars",
                                             "key", "free", "dbg")},
                   params={k: (m.get("params") or {}).get(k) for k in ("only", "prefer", "mode", "bars")})
        old.append(json.dumps(rec, ensure_ascii=False))
    if old:
        root.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(old) + "\n", "utf8")
    elif log.exists():
        log.unlink()
    update_loop_meta(dig, name, wrong=bool(on))
    return bool(on)


def safe_path(rel: str) -> Path | None:
    """A path inside the output folder, or None if `rel` tries to escape it."""
    root = out_root().resolve()
    p = (root / rel).resolve()
    if root == p or root in p.parents:
        return p
    return None


def run_worker(job, st: core.JobState):
    """Thread of one job. Every job has its own JobState: after "Stop" the thread may keep writing its
    summary for a few seconds, but by then a new dig already runs with its own state."""
    try:
        job(st)
        if st.found_cookies:             # remember the browser whose cookies worked
            SETTINGS.setdefault("last", {})["cookies_browser"] = st.found_cookies
            save_settings(SETTINGS)
        with st.lock:
            st.status = "stopped" if st.stop else "done"
            st.progress = 1.0
            st.current = ""
    except core.Stopped:                 # "Stop" in the middle of a network request
        with st.lock:
            st.status, st.current = "stopped", ""
    except Exception as e:
        traceback.print_exc()
        st.log(T("✕ Error: {e}", e=str(e)[:400]))
        with st.lock:
            st.status = "stopped" if st.stop else "error"
            st.current = ""
    finally:
        with st.lock:
            st.busy = False


def read_dig(dig: str) -> dict:
    """dig.json of a crate ({} if missing or broken)."""
    p = safe_path(f"{dig}/dig.json")
    try:
        return json.loads(p.read_text("utf8"))
    except Exception:
        return {}


def get_loop(dig: str, name: str) -> dict | None:
    """The loop entry `name` in a crate (or None)."""
    for t in read_dig(dig).get("tracks", []):
        for l in t["loops"]:
            if l["name"] == name:
                return l
    return None


def build_ms_zip(items: list[dict]) -> tuple[Path, int, int]:
    """ZIP for the Model:Samples:  <BPM>/<key>/<loop>.wav — exactly the same files that play on screen
    (stereo if stereo was saved). Elektron Transfer converts them to mono.
    -> (path of the zip, how many loops were added, how many were not found)"""
    import zipfile
    tmp = out_root() / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    zpath = tmp / f"export_{uuid.uuid4().hex[:8]}.zip"
    used, added, missing = set(), 0, 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:   # wav barely compresses — storing is faster
        for it in items:
            dig, name = str(it.get("dig", "")), str(it.get("name", ""))
            l = get_loop(dig, name)
            if not l:
                missing += 1
                continue
            files = l.get("files") or {}
            src = None
            for kind in ("stereo", "mono"):             # same order as the player on screen
                if files.get(kind):
                    cand = safe_path(f"{dig}/{files[kind]}")
                    if cand and cand.exists():
                        src = cand
                        break
            if src is None:
                missing += 1
                continue
            arc_dir = f"{int(l['bpm'])}/{l['key']}"
            arc = f"{arc_dir}/{name}.wav"
            k = 2
            while arc in used:                          # equal names from different crates
                arc = f"{arc_dir}/{name}_{k}.wav"
                k += 1
            z.write(src, arc)
            used.add(arc)
            added += 1
    return zpath, added, missing


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def jam_name(l: dict, rate: float, bpm: int) -> str:
    """Name of a loop after it is pitched by `rate` to `bpm`: the key and BPM at the start of the name
    change, the rest stays. The key moves by the pitch shift (plus the record's own tuning), rounded
    to the nearest semitone: Amaj80PTitle1 sped up to 95 BPM -> Cmaj95PTitle1."""
    m = re.match(r"([A-G]#?)(maj|min)$", l.get("key", ""))
    head = f"{l.get('key')}{l.get('bpm')}"
    if not m or not l["name"].startswith(head):
        return l["name"]
    import math
    semis = round((1200 * math.log2(rate) + (l.get("tuning_cents") or 0)) / 100)
    key = NOTE_NAMES[(NOTE_NAMES.index(m.group(1)) + semis) % 12] + m.group(2)
    return f"{key}{bpm}{l['name'][len(head):]}"


def build_jam_zip(items: list[dict]) -> tuple[Path, int, str]:
    """"Download jam": the loops playing right now, each pitched exactly as the jam plays it (speed =
    master BPM / loop BPM), renamed to its new key and BPM. items: [{dig, name, rate}], master first.
    -> (path of the zip, number of loops, zip name "<master key><BPM>-<date>-<time>.zip")"""
    import zipfile
    loops = [(get_loop(str(it.get("dig", "")), str(it.get("name", ""))), it) for it in items]
    loops = [(l, it) for l, it in loops if l]
    if not loops:
        return None, 0, ""
    master = loops[0][0]
    mbpm = int(master["bpm"])
    tmp = out_root() / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    zpath = tmp / f"jam_{uuid.uuid4().hex[:8]}.zip"
    used, added = set(), 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for l, it in loops:
            files = l.get("files") or {}
            src = next((safe_path(f"{it['dig']}/{files[k]}") for k in ("stereo", "mono") if files.get(k)), None)
            if not (src and src.exists()):
                continue
            rate = min(2.0, max(0.5, float(it.get("rate") or 1.0)))
            if abs(rate - 1.0) < 1e-6:                     # the master (or "Tempo from first loop" is off)
                data, name = src.read_bytes(), l["name"]
            else:
                x = dsp.read_wav(src)
                bpm = int(round(l["bpm"] * rate))
                out = dsp.pitch_loop(x, rate, int(round(len(x) / rate)))
                wav = tmp / f"jam_{uuid.uuid4().hex[:8]}.wav"
                dsp.write_wav(wav, out, bits=24 if src.parent.name == core.STEREO_DIR else 16)
                data, name = wav.read_bytes(), jam_name(l, rate, bpm)
                wav.unlink(missing_ok=True)
            arc, k = f"{name}.wav", 2
            while arc in used:
                arc, k = f"{name}_{k}.wav", k + 1
            z.writestr(arc, data)
            used.add(arc)
            added += 1
    return zpath, added, f"{master['key']}{mbpm}-{time.strftime('%Y-%m-%d-%H-%M')}.zip"


def update_loop_meta(dig: str, name: str, **changes):
    """Change fields of a loop in dig.json (and in the running job, if it is that crate)."""
    p = safe_path(f"{dig}/dig.json")
    if not p or not p.exists():
        return
    m = json.loads(p.read_text("utf8"))
    for t in m.get("tracks", []):
        for l in t["loops"]:
            if l["name"] == name:
                l.update(changes)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), "utf8")
    with STATE.lock:
        if STATE.dig_name == dig:
            STATE.tracks = m["tracks"]


class Handler(BaseHTTPRequestHandler):
    """HTTP handler: the page, the JSON API and loop files."""
    server_version = "CrateDigger"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        """Send a JSON response."""
        b = json.dumps(obj, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        """The JSON body of a POST request ({} if empty or broken)."""
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf8"))
        except Exception:
            return {}

    def _file(self, path: Path, ctype=None):
        """Send a file from disk."""
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _start(self, job):
        """Start a job in the background: a dig, own files or a re-run — one at a time."""
        global WORKER, STATE
        if WORKER and WORKER.is_alive() and not STATE.stop:      # a stopped job doesn't block
            return self._json({"error": T("wait until the dig is finished")}, 409)
        st = core.JobState()
        st.status, st.busy = "running", True
        STATE = st
        WORKER = threading.Thread(target=run_worker, args=(job, st), daemon=True)
        WORKER.start()
        return self._json({"ok": True})

    def _upload(self):
        """Receive an audio file dragged into the window; it is stored under .tmp/uploads."""
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "file.mp3")
        ext = Path(name).suffix.lower()
        if ext not in (".mp3", ".wav", ".flac", ".aif", ".aiff", ".m4a", ".ogg", ".opus", ".webm", ".aac"):
            return self._json({"error": T("not an audio file: {name}", name=name)}, 400)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 800 * 1024 * 1024:
            return self._json({"error": T("the file is empty or larger than 800 MB")}, 400)
        up = out_root() / ".tmp" / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        tok = uuid.uuid4().hex
        with open(up / (tok + ext), "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        return self._json({"token": tok, "name": name})

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        """Routes: the page, /api/* reads, /files/* loop audio."""
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._file(APP_DIR / "ui.html", "text/html; charset=utf-8")
            if u.path == "/api/ping":
                return self._json({"app": "cratedigger"})
            if u.path == "/api/state":
                since = int((q.get("since") or ["0"])[0])
                snap = STATE.snapshot(since)
                session_add(snap.get("dig"))
                return self._json(snap)
            if u.path == "/api/session":
                with SESSION_LOCK:
                    digs, hidden = list(SESSION["digs"]), sorted(SESSION["hidden"])
                out = []
                running = STATE.snapshot(10 ** 9)
                for d in digs:
                    if d == FAV_KEY:
                        out.append({"name": d, "tracks": favorites(), "running": False})
                    elif d == running["dig"] and running["status"] == "running":
                        out.append({"name": d, "tracks": running["tracks"], "running": True})
                    else:
                        m = read_dig(d)
                        if m:
                            out.append({"name": d, "tracks": m.get("tracks", []), "running": False})
                return self._json({"digs": out, "hidden": hidden})
            if u.path == "/api/health":
                return self._json(health())
            if u.path == "/api/settings":
                return self._json({"out_dir": str(out_root()), "last": SETTINGS.get("last", {}),
                                   "defaults": core.DEFAULTS, "lang": SETTINGS.get("lang"),
                                   "debug": bool(SETTINGS.get("debug"))})
            if u.path == "/api/digs":
                favs = sum(len(t["loops"]) for t in favorites())
                return self._json({"digs": list_digs(), "favs": favs, "fav_key": FAV_KEY})
            if u.path == "/api/favorites":
                return self._json({"tracks": favorites()})
            if u.path == "/api/dig":
                name = (q.get("name") or [""])[0]
                p = safe_path(f"{name}/dig.json")
                if p and p.exists():
                    return self._json(json.loads(p.read_text("utf8")))
                return self._json({"error": T("no such crate")}, 404)
            if u.path.startswith("/files/"):
                p = safe_path(urllib.parse.unquote(u.path[len("/files/"):]))
                if p:
                    return self._file(p)
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        """Routes: jobs (dig, own files, re-run, stop), settings, session, ZIP, favourites, wrong-match feedback."""
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/upload":
            return self._upload()
        b = self._body()
        try:
            if u.path == "/api/dig":
                params = core.normalize_params(b)
                SETTINGS["last"] = params
                save_settings(SETTINGS)
                return self._start(lambda st: core.run_dig(params, out_root(), st))
            if u.path == "/api/dig_local":
                up = out_root() / ".tmp" / "uploads"
                files = []
                for f in b.get("files") or []:
                    tok = re.sub(r"[^a-f0-9]", "", str(f.get("token", "")))
                    cand = list(up.glob(tok + ".*")) if tok else []
                    if cand:
                        files.append((cand[0], str(f.get("name") or cand[0].name)))
                if not files:
                    return self._json({"error": T("the files did not upload")}, 400)
                params = core.normalize_params(b.get("params") or {})
                return self._start(lambda st: core.run_local(files, params, out_root(), st))
            if u.path == "/api/rerun":               # a record was dragged from the list into the left column
                dig, tid = str(b.get("dig", "")), str(b.get("id", ""))
                d = safe_path(dig) if dig else None
                track = next((t for t in read_dig(dig).get("tracks", []) if t.get("id") == tid), None) if d else None
                if not track:
                    return self._json({"error": T("this record was not found in the crate folder")}, 404)
                if (track.get("local") or not track.get("url")) and not core.find_source(d, track):
                    return self._json({"error": T("the source file was not kept — drag it into the "
                                                  "window again")}, 400)
                params = core.normalize_params(b.get("params") or {})
                return self._start(lambda st: core.run_rerun(d, track, params, out_root(), st))
            if u.path == "/api/stop":
                STATE.request_stop()                 # also kills the analysis processes at once
                return self._json({"ok": True})
            if u.path == "/api/settings":
                if "debug" in b:                       # "Debugging" checkbox in More settings
                    SETTINGS["debug"] = bool(b["debug"])
                    save_settings(SETTINGS)
                if b.get("lang") in i18n.LANGS:        # flag in the header: language of server messages and files
                    i18n.set_lang(b["lang"])
                    SETTINGS["lang"] = b["lang"]
                    save_settings(SETTINGS)
                if b.get("out_dir"):
                    p = Path(b["out_dir"]).expanduser()
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                        SETTINGS["out_dir"] = str(p)
                        save_settings(SETTINGS)
                    except Exception as e:
                        return self._json({"error": T("can't create the folder: {e}", e=e)}, 400)
                return self._json({"ok": True, "out_dir": str(out_root())})
            if u.path == "/api/open":
                dig = b.get("dig") or ""
                p = safe_path(dig) if dig else out_root()
                if b.get("sub") and p:
                    sp = p / b["sub"]
                    p = sp if sp.exists() else p
                if p and p.exists():
                    open_in_os(p)
                return self._json({"ok": True})
            if u.path == "/api/session":
                act, key = b.get("action"), f"{b.get('dig', '')}/{b.get('name', '')}"
                with SESSION_LOCK:
                    if act == "add":
                        if b.get("dig") and b["dig"] not in SESSION["digs"]:
                            SESSION["digs"].append(b["dig"])
                    elif act == "hide":
                        SESSION["hidden"].add(key)
                    elif act == "unhide":
                        SESSION["hidden"].discard(key)
                    elif act == "remove_dig":
                        if b.get("dig") in SESSION["digs"]:
                            SESSION["digs"].remove(b["dig"])
                    elif act == "clear":
                        SESSION["digs"].clear()
                        SESSION["hidden"].clear()
                return self._json({"ok": True})
            if u.path == "/api/jam_zip":           # "Download jam": playing loops, pitched like in the jam
                zpath, added, zname = build_jam_zip(b.get("items") or [])
                if not added:
                    if zpath:
                        zpath.unlink(missing_ok=True)
                    return self._json({"error": T("loop files not found on disk")}, 404)
                data = zpath.read_bytes()
                zpath.unlink(missing_ok=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f'attachment; filename="{zname}"')
                self.send_header("X-Loops", str(added))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/zip":
                items = b.get("items") or []
                if not items:
                    return self._json({"error": T("there are no loops on screen")}, 400)
                zpath, added, missing = build_ms_zip(items)
                if not added:
                    zpath.unlink(missing_ok=True)
                    return self._json({"error": T("loop files not found on disk")}, 404)
                stamp = time.strftime("%Y-%m-%d_%H-%M")
                size = zpath.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="CrateDigger_ModelSamples_{stamp}.zip"')
                self.send_header("X-Loops", str(added))
                self.send_header("X-Missing", str(missing))
                self.end_headers()
                try:
                    with open(zpath, "rb") as f:
                        while True:
                            chunk = f.read(1 << 20)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                finally:
                    zpath.unlink(missing_ok=True)
                return
            if u.path == "/api/wrong":              # Debugging mode: 👎 wrong match — set or remove
                wrong = set_wrong(str(b.get("dig", "")), str(b.get("name", "")), bool(b.get("on", True)))
                if wrong is None:
                    return self._json({"error": T("the loop's files were not found on disk")}, 404)
                return self._json({"ok": True, "wrong": wrong, "folder": str(out_root() / FEEDBACK_DIR)})
            if u.path == "/api/fav":                # the star: set or remove
                fav = set_favorite(str(b.get("dig", "")), str(b.get("name", "")), bool(b.get("on", True)))
                if fav is None:
                    return self._json({"error": T("the loop's files were not found on disk")}, 404)
                return self._json({"ok": True, "fav": fav})
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


def find_port(start=8765):
    """A free local port (or the port of an already running CrateDigger). -> (port, already_running)"""
    for port in range(start, start + 30):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.4) as r:
                if json.loads(r.read()).get("app") == "cratedigger":
                    return port, True
        except Exception:
            pass
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port, False
            except OSError:
                continue
    raise RuntimeError(T("no free port"))


def open_window(url: str):
    """Try to open as a separate window (Chrome/Edge in app mode)."""
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                          Path(base) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    else:
        for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            w = shutil.which(n)
            if w:
                cands.append(Path(w))
    for c in cands:
        if c.exists():
            try:
                subprocess.Popen([str(c), f"--app={url}", "--window-size=1320,900"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                continue
    webbrowser.open(url)


def main():
    """Start the server and open the window."""
    port, running = find_port()
    url = f"http://127.0.0.1:{port}/"
    if running:
        print(T("CrateDigger is already running — opening the window."))
        open_window(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 56)
    print(T("  CrateDigger {v} is running:  {url}", v=VERSION, url=url))
    print(T("  Loops are saved to: {d}", d=out_root()))
    print(T("  Keep this window open while digging."))
    print("=" * 56)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, open_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
