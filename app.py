"""
CrateDigger — копание семплов для бум-бэпа.
Запускает локальный сервер и открывает интерфейс в окне браузера.
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
FAV_DIR = "Favorites"          # копии избранных лупов на диске
FAV_KEY = "★favorites"         # «ящик» Избранное на экране: собирается из всех ящиков
VERSION = "1.2.2"


def default_out_dir() -> Path:
    home = Path.home()
    music = home / "Music"
    return (music if music.exists() else home) / "CrateDigger"


def load_settings() -> dict:
    s = {"out_dir": str(default_out_dir()), "last": {}}
    try:
        s.update(json.loads(SETTINGS_PATH.read_text("utf8")))
    except Exception:
        pass
    if s.get("v", 0) < 12:                     # 1.2: по умолчанию теперь «Только стерео 24 бит»
        if s["last"].get("formats") == "both":
            s["last"]["formats"] = "stereo"
        s["v"] = 12
    if s.get("lang") not in i18n.LANGS:      # язык ещё не выбирали — окно возьмёт язык браузера
        s.pop("lang", None)
    return s


def save_settings(s: dict):
    try:
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf8")
    except Exception:
        pass


SETTINGS = load_settings()
i18n.set_lang(SETTINGS.get("lang") or "ru")
STATE = core.JobState()
WORKER: threading.Thread | None = None
# что сейчас на экране: ящики этой рабочей сессии (пока программа запущена) и убранные лупы
SESSION = {"digs": [], "hidden": set()}
SESSION_LOCK = threading.Lock()


def session_add(dig: str):
    if not dig:
        return
    with SESSION_LOCK:
        if dig not in SESSION["digs"]:
            SESSION["digs"].append(dig)


def out_root() -> Path:
    p = Path(SETTINGS.get("out_dir") or default_out_dir()).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def open_in_os(path: Path):
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
    """(имя ящика, dig.json) — от новых к старым."""
    root = out_root()
    for d in sorted([p for p in root.iterdir() if p.is_dir() and (p / "dig.json").exists()],
                    key=lambda p: p.name, reverse=True):
        try:
            yield d.name, json.loads((d / "dig.json").read_text("utf8"))
        except Exception:
            continue


def list_digs():
    out = []
    for name, m in all_digs():
        if len(out) >= 80:
            break
        n = sum(len(t["loops"]) for t in m.get("tracks", []))
        out.append(dict(name=name, loops=n, records=len(m.get("tracks", []))))
    return out


def favorites() -> list[dict]:
    """Всё, что когда-либо отмечено звёздочкой: пластинки с одними избранными лупами.
    У каждой пластинки поле dig — настоящий ящик, где лежат её файлы."""
    out = []
    for name, m in all_digs():
        for t in m.get("tracks", []):
            loops = [l for l in t.get("loops", []) if l.get("fav")]
            if loops:
                out.append(dict(t, loops=loops, dig=name))
    return out


def set_favorite(dig: str, name: str, on: bool) -> bool | None:
    """Звёздочка: копия лупа в Favorites или снятие метки (копия удаляется). None — луп не найден."""
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
            while dst.exists() and not filecmp.cmp(dst, src, shallow=False):   # другой луп с тем же именем
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
    for rel in l.get("fav_files") or list((l.get("files") or {}).values()):   # у старых — по прежней схеме
        f = (fav_root / rel).resolve()
        if root not in f.parents:
            continue
        f.unlink(missing_ok=True)
        for d in f.parents:                        # пустые папки внутри Favorites тоже убираем
            if d == root:
                break
            try:
                d.rmdir()
            except OSError:
                break
    update_loop_meta(dig, name, fav=False, fav_files=[])
    return False


def safe_path(rel: str) -> Path | None:
    root = out_root().resolve()
    p = (root / rel).resolve()
    if root == p or root in p.parents:
        return p
    return None


def run_worker(job, st: core.JobState):
    """Поток одной задачи. У каждой задачи свой JobState: после «Стоп» поток может ещё
    несколько секунд дописывать итог, но новое копание к этому времени уже идёт со своим."""
    try:
        job(st)
        if st.found_cookies:             # запоминаем браузер, cookies которого сработали
            SETTINGS.setdefault("last", {})["cookies_browser"] = st.found_cookies
            save_settings(SETTINGS)
        with st.lock:
            st.status = "stopped" if st.stop else "done"
            st.progress = 1.0
            st.current = ""
    except core.Stopped:                 # «Остановить» посреди сетевого запроса
        with st.lock:
            st.status, st.current = "stopped", ""
    except Exception as e:
        traceback.print_exc()
        st.log(T("✕ Ошибка: {e}", e=str(e)[:400]))
        with st.lock:
            st.status = "stopped" if st.stop else "error"
            st.current = ""
    finally:
        with st.lock:
            st.busy = False


def read_dig(dig: str) -> dict:
    p = safe_path(f"{dig}/dig.json")
    try:
        return json.loads(p.read_text("utf8"))
    except Exception:
        return {}


def get_loop(dig: str, name: str) -> dict | None:
    for t in read_dig(dig).get("tracks", []):
        for l in t["loops"]:
            if l["name"] == name:
                return l
    return None


def build_ms_zip(items: list[dict]) -> tuple[Path, int, int]:
    """ZIP под Model:Samples:  <BPM>/<тональность>/<луп>.wav — файлы ровно те же, что
    играют на экране (стерео, если сохранялось стерео). В моно их переведёт Elektron Transfer.
    -> (путь к zip, сколько лупов положили, сколько не нашли)"""
    import zipfile
    tmp = out_root() / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    zpath = tmp / f"export_{uuid.uuid4().hex[:8]}.zip"
    used, added, missing = set(), 0, 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:   # wav почти не сжимается — так быстрее
        for it in items:
            dig, name = str(it.get("dig", "")), str(it.get("name", ""))
            l = get_loop(dig, name)
            if not l:
                missing += 1
                continue
            files = l.get("files") or {}
            src = None
            for kind in ("stereo", "mono"):             # тот же порядок, что у плеера на экране
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
            while arc in used:                          # одинаковые имена из разных ящиков
                arc = f"{arc_dir}/{name}_{k}.wav"
                k += 1
            z.write(src, arc)
            used.add(arc)
            added += 1
    return zpath, added, missing


def update_loop_meta(dig: str, name: str, **changes):
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
    server_version = "CrateDigger"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf8"))
        except Exception:
            return {}

    def _file(self, path: Path, ctype=None):
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
        """Запуск задачи в фоне: копание, свои файлы или повторный прогон — по одной за раз."""
        global WORKER, STATE
        if WORKER and WORKER.is_alive() and not STATE.stop:      # остановленная задача не мешает
            return self._json({"error": T("дождитесь окончания копания")}, 409)
        st = core.JobState()
        st.status, st.busy = "running", True
        STATE = st
        WORKER = threading.Thread(target=run_worker, args=(job, st), daemon=True)
        WORKER.start()
        return self._json({"ok": True})

    def _upload(self):
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "file.mp3")
        ext = Path(name).suffix.lower()
        if ext not in (".mp3", ".wav", ".flac", ".aif", ".aiff", ".m4a", ".ogg", ".opus", ".webm", ".aac"):
            return self._json({"error": T("не аудиофайл: {name}", name=name)}, 400)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 800 * 1024 * 1024:
            return self._json({"error": T("файл пустой или больше 800 МБ")}, 400)
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
                                   "defaults": core.DEFAULTS, "lang": SETTINGS.get("lang")})
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
                return self._json({"error": T("нет такого ящика")}, 404)
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
                    return self._json({"error": T("файлы не загрузились")}, 400)
                params = core.normalize_params(b.get("params") or {})
                return self._start(lambda st: core.run_local(files, params, out_root(), st))
            if u.path == "/api/rerun":               # пластинку перетащили из списка в левую колонку
                dig, tid = str(b.get("dig", "")), str(b.get("id", ""))
                d = safe_path(dig) if dig else None
                track = next((t for t in read_dig(dig).get("tracks", []) if t.get("id") == tid), None) if d else None
                if not track:
                    return self._json({"error": T("эта пластинка не нашлась в папке ящика")}, 404)
                if (track.get("local") or not track.get("url")) and not core.find_source(d, track):
                    return self._json({"error": T("исходный файл не сохранился — перетащите его в окно ещё раз")}, 400)
                params = core.normalize_params(b.get("params") or {})
                return self._start(lambda st: core.run_rerun(d, track, params, out_root(), st))
            if u.path == "/api/stop":
                STATE.request_stop()                 # и сразу останавливает процессы анализа
                return self._json({"ok": True})
            if u.path == "/api/settings":
                if b.get("lang") in i18n.LANGS:        # флажок в шапке: язык сообщений сервера и файлов
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
                        return self._json({"error": T("не могу создать папку: {e}", e=e)}, 400)
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
            if u.path == "/api/zip":
                items = b.get("items") or []
                if not items:
                    return self._json({"error": T("на экране нет лупов")}, 400)
                zpath, added, missing = build_ms_zip(items)
                if not added:
                    zpath.unlink(missing_ok=True)
                    return self._json({"error": T("файлы лупов не найдены на диске")}, 404)
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
            if u.path == "/api/fav":                # звёздочка: поставить или снять
                fav = set_favorite(str(b.get("dig", "")), str(b.get("name", "")), bool(b.get("on", True)))
                if fav is None:
                    return self._json({"error": T("файлы лупа не найдены на диске")}, 404)
                return self._json({"ok": True, "fav": fav})
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


def find_port(start=8765):
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
    raise RuntimeError(T("нет свободного порта"))


def open_window(url: str):
    """Пытаемся открыть как отдельное окно (Chrome/Edge в режиме приложения)."""
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
    port, running = find_port()
    url = f"http://127.0.0.1:{port}/"
    if running:
        print(T("CrateDigger уже запущен — открываю окно."))
        open_window(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 56)
    print(T("  CrateDigger {v} работает:  {url}", v=VERSION, url=url))
    print(T("  Лупы сохраняются в: {d}", d=out_root()))
    print(T("  Не закрывайте это окно, пока копаете."))
    print("=" * 56)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, open_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
