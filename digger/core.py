"""
Главный процесс «копания»: поиск -> проверка эпохи -> скачивание -> анализ -> лупы -> файлы.
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
PREFER_RU = {"any": "любые лупы", "breaks": "брейки", "melodic": "мелодия"}
ONLY_RU = {"drums": "одни барабаны", "bass": "один бас", "vocal": "голос без бита", "pads": "одни пэды"}


def what_ru(p: dict) -> str:
    """Что ищем — для журнала."""
    return T(" или ").join(T(iso.NAME_RU[k]) for k in p["only"]) if p["only"] else T(PREFER_RU[p["prefer"]])

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def translit(s: str) -> str:
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
    s = translit(s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "random"


def safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip(" .")[:120] or "track"


# --------------------------------------------------------------------------- состояние
class JobState:
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
        self.note = ""          # итог, который надо показать вместо «Готово»
        self.stop_hooks = []    # что сделать сразу по «Стоп» (прибить процессы анализа)
        self.busy = False       # поток задачи ещё дорабатывает (после «Стоп» — уже не мешает новой)

    def request_stop(self):
        """«Остановить»: окно сразу свободно для нового копания. Сеть бросает запрос, процессы
        анализа прибиваются, а поток задачи дописывает итог в этот (уже старый) JobState."""
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
        with self.lock:
            self.log_lines.append(time.strftime("%H:%M:%S ") + msg)
            if len(self.log_lines) > 600:
                self.log_lines = self.log_lines[-500:]

    def snapshot(self, since=0):
        with self.lock:
            return dict(status=self.status, progress=round(self.progress, 3), current=self.current,
                        log=self.log_lines[since:], log_total=len(self.log_lines),
                        tracks=self.tracks, dig=self.dig_name, found_cookies=self.found_cookies,
                        note=self.note, busy=self.busy)


# --------------------------------------------------------------------------- история
class History:
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


# --------------------------------------------------------------------------- копаем
DEFAULTS = dict(tags="", n_tracks=5, n_loops=3, bars=2, prefer="any", mode="stretch",
                bpm_min=60, bpm_max=120, y0=1960, y1=1999, strict_year=False,
                year_filter=True, bpm_filter=True,
                save_mp3=True, formats="stereo", chops=False, normalize=True, title_len=20,
                cookies_browser="", rare=False, sort_ms=False, only=[])


def _int(v, lo, hi, d):
    try:
        return max(lo, min(hi, int(float(v))))
    except Exception:
        return d


def normalize_params(p: dict) -> dict:
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
    # «Только …» — можно несколько сразу (работают как ИЛИ); первый ряд тогда не важен
    only = q["only"] or []
    if isinstance(only, str):
        only = only.split(",")
    if q["prefer"] == "drums":          # настройки версии 1.0
        only, q["prefer"] = list(only) + ["drums"], "any"
    q["only"] = [k for k in iso.KINDS if k in only]
    q["prefer"] = q["prefer"] if q["prefer"] in PREFER_RU else "any"
    if q["only"] and q["bars"] == 4:    # «Только …» — для лупов в 1–2 такта
        q["prefer"] = "breaks" if "drums" in q["only"] else "any"
        q["only"] = []
    q["mode"] = q["mode"] if q["mode"] in ("stretch", "vinyl") else "stretch"
    q["formats"] = q["formats"] if q["formats"] in ("both", "stereo", "mono") else "stereo"
    for k in ("strict_year", "save_mp3", "chops", "normalize", "rare", "sort_ms", "year_filter", "bpm_filter"):
        q[k] = bool(q[k]) and q[k] not in ("0", "false", "False")
    if not q["bpm_filter"]:                            # без фильтра — весь диапазон 60–120
        q["bpm_min"], q["bpm_max"] = 60, 120
    return q


def unique_name(base: str, taken: set) -> str:
    if base not in taken:
        return base
    for suf in "bcdefghijklmnopqrstuvwxyz":
        if base + suf not in taken:
            return base + suf
    return base + str(random.randint(100, 999))


def _new_dig_dir(root: Path, label: str) -> tuple[Path, str]:
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
    return dict(mode=p["mode"], bpm_min=p["bpm_min"], bpm_max=p["bpm_max"],
                fixed_bpm=None, max_stretch=0.05, normalize=p["normalize"])


def _max_stretch(p: dict, kind: str | None) -> float:
    """Насколько можно подгонять темп. Вокалу в «стретче» — сколько нужно (высота не меняется),
    в «питчинге» — до ±15 %; остальным — до ±5 %."""
    if kind == "vocal":
        return 0.15 if p["mode"] == "vinyl" else 1.0
    return 0.05


def cut_track(path, p: dict, hint_bpm: int | None = None, lenient: bool = False, tmp_dir=None) -> dict:
    """Анализ одного трека и нарезка лупов БЕЗ записи результата на диск — чистая функция
    (её запускают процессы конвейера). hint_bpm — самый частый BPM в ящике (по нему режем
    пэды из треков без бита). lenient — брать сколько нашлось. tmp_dir — куда ffmpeg временно
    кладёт раскодированный звук (тогда он не занимает память процесса).
    -> dict(loops=[…с аудио…], logs=[…], reason=None | «почему трек не подошёл», tempo, tuning)"""
    set_lang(p.get("lang"))              # процесс анализа пишет причины на языке интерфейса
    x = dsp.load_audio_i16(path, tmp_dir=tmp_dir)
    try:
        return _cut(x, p, hint_bpm, lenient)
    finally:
        dsp.release_audio(x)


def _cut(x, p: dict, hint_bpm, lenient) -> dict:
    t_start = time.time()
    an = dsp.TrackAnalysis(x, want_iso=any(k in iso.ISO_KINDS for k in p["only"]))
    res = dict(loops=[], reason=None, tempo=round(an.tempo, 2), tuning=round(an.tuning, 1),
               logs=[T("   темп ≈{tempo:.1f} BPM, строй {tuning:+.0f} центов, анализ {sec:.1f} с",
                       tempo=an.tempo, tuning=an.tuning, sec=time.time() - t_start)])
    only, n = p["only"], p["n_loops"]
    free_ok = any(k in ("pads", "vocal") for k in only)   # им сетка и точный темп не обязательны
    why = an.rhythm_problem()
    if why and not free_ok:
        res["reason"] = T("{why} — ровно не залупится", why=why)
        return res
    track_fit = dsp.choose_target(an.tempo, p["bpm_min"], p["bpm_max"], None, 0.05)
    if track_fit is None and not free_ok:
        res["reason"] = T("темп {tempo:.1f} не подходит под диапазон {a}–{b}", tempo=an.tempo, a=p["bpm_min"], b=p["bpm_max"])
        return res

    if only:
        free_bpm = None
        if free_ok:
            if not why and track_fit:
                free_bpm = track_fit[0]
            elif hint_bpm and p["bpm_min"] <= hint_bpm <= p["bpm_max"]:
                free_bpm = int(hint_bpm)
            else:
                free_bpm = 5 * round((p["bpm_min"] + p["bpm_max"]) / 10)
        cands = [c for k in only for c in an.iso_candidates(k, p["bars"], free_bpm)]
        picks = dsp.select_loops(cands, n + 4, sims=(0.93, 0.975, 0.985))
    else:
        free_bpm = None
        picks = an.pick_loops(n + 4, p["bars"], p["prefer"])

    # один целевой темп на всю пластинку — лупы одного трека сойдутся друг с другом
    opts, fixed = _loop_opts(p), None
    grid = [c for c in picks[:n] if not c.get("free")]
    if grid:
        bt = an.beat_times
        reals = [60.0 * (c["e"] - c["s"]) / (bt[c["e"]] - bt[c["s"]]) for c in grid]
        fixed = dsp.choose_target(float(np.median(reals)), p["bpm_min"], p["bpm_max"], None, 0.05)
    free_target = fixed[0] if fixed else free_bpm

    rendered = []
    for c in picks:
        if len(rendered) >= n:
            break
        kind = c.get("kind")
        if c.get("free"):
            r = dsp.render_free(an, c["t0"], p["bars"], free_target, dict(opts, kind=kind))
        else:
            ms = _max_stretch(p, kind)
            o = dict(opts, kind=kind, max_stretch=ms)
            if fixed:
                o.update(fixed_bpm=fixed[0], max_stretch=max(0.07, ms))
            r = dsp.render_loop(an, c["s"], c["e"], o)
            if r is None and kind == "pads" and free_target:      # пэд не влез в темп — режем без растяжения
                r = dsp.render_free(an, c["t0"], p["bars"], free_target, dict(opts, kind=kind))
        if r:
            r["score"] = round(float(c.get("rank", c.get("score", 0))), 3)
            if kind:
                r.update(kind=kind, mixed=c.get("grade", 2) < 2)
            rendered.append(r)

    need = 1 if only else n               # изолированные куски редкие — хватит и одного
    if len(rendered) < need and not lenient:
        res["reason"] = (T("нет мест, где звучит {what} — беру другую пластинку",
                           what=T(" или ").join(T(ONLY_RU[k]) for k in only))
                         if only else T("нашёл только {k} хороших лупа(ов) из {n}", k=len(rendered), n=n))
        return res
    if not rendered:
        res["reason"] = T("не нашёл ни одного ровного лупа")
        return res
    rendered.sort(key=lambda r: r["src_start"])
    res["loops"] = rendered
    return res


class Session:
    """Один «ящик»: папка, метаданные, уникальные имена."""

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
        self.log(T("📦 Новый ящик: {name}", name=self.dig_dir.name))
        self.log(T("🎚 Подгонка темпа: питчинг (темп меняется вместе с высотой)") if p["mode"] == "vinyl"
                 else T("🎚 Подгонка темпа: стретч (удары на месте, высота та же)"))

    def common_bpm(self) -> int | None:
        """Самый частый BPM среди лупов этого ящика."""
        with self.lock:
            c = Counter(l["bpm"] for t in self.meta["tracks"] for l in t["loops"])
        return c.most_common(1)[0][0] if c else None

    def save(self, path: Path, track: dict, res: dict):
        """Запись лупов на диск и в dig.json (только в главном процессе — тут уникальные имена)."""
        p, log = self.p, self.log
        label = f"{track['artist']} — {track['title']}"
        short = clean_title(track["title"], p["title_len"])
        track.update(tempo=res["tempo"], tuning=res["tuning"], loops=[])
        dig_dir = self.dig_dir
        for i, r in enumerate(res["loops"], 1):
            letter = iso.LETTER.get(r.get("kind"), "")          # Amin95BQuark1 — тип после тональности
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
        """Исходник рядом с лупами — из него пластинку можно прогнать заново без YouTube."""
        d = self.dig_dir / SRC_DIR
        d.mkdir(exist_ok=True)
        base = safe_name(label.replace(" — ", " - "))
        try:
            if path.suffix.lower() in KEEP_AS_IS:          # mp3/wav/flac копируем без перекодирования
                dst = d / (base + path.suffix.lower())
                shutil.copy2(path, dst)
            else:
                dst = d / (base + ".mp3")
                dsp.save_mp3(path, dst)
            track["src"] = f"{SRC_DIR}/{dst.name}"
        except Exception as e:
            self.log(T("   (исходник не сохранился: {e})", e=str(e)[:120]))

    def finish(self, n_records):
        write_meta(self.dig_dir, self.meta)
        total = sum(len(t["loops"]) for t in self.meta["tracks"])
        if total == 0:
            shutil.rmtree(self.dig_dir, ignore_errors=True)
            with self.state.lock:
                self.state.dig_name = ""
        self.log(T("🏁 Готово: {n} {rw}, {total} {lw}", n=n_records, rw=P(n_records, "record"),
                   total=total, lw=P(total, "loop")) + (f" → {self.dig_dir}" if total else ""))
        return self.dig_dir if total else None


class Pipeline:
    """Конвейер: треки анализируются в отдельных процессах (workers.Analyzer), а результаты
    записывает только этот процесс — в одном потоке записи, по очереди (без гонок за имена
    и dig.json). Сеть тем временем скачивает следующий трек."""

    def __init__(self, sess: "Session", state: JobState, total: int, lenient: bool = False, on_accept=None):
        self.sess, self.state, self.p = sess, state, sess.p
        self.total, self.lenient, self.on_accept = total, lenient, on_accept
        self.log = state.log
        self.pool = workers.Analyzer(self.log, tasks=total, tmp=sess.root / ".tmp" / "analysis")
        if self.pool.parallel and self.pool.n > 1:
            self.log(T("⚙ Анализ идёт в {n} процессах параллельно", n=self.pool.n))
        self.cv = threading.Condition()
        self.pending: dict[int, str] = {}         # отдано в анализ, ещё не записано: номер -> название
        self.accepted = 0
        self.net = ""                             # что сейчас делает сеть (для строки состояния)
        self._n = 0
        self.q: queue.Queue = queue.Queue()
        self.saver = threading.Thread(target=self._save_loop, daemon=True, name="saver")
        self.saver.start()
        state.stop_hooks.append(self.pool.kill)  # «Стоп» — сразу, даже если сеть занята скачиванием

    @property
    def slots(self) -> int:
        """Сколько треков держать в анализе одновременно."""
        return self.pool.n if self.pool.parallel else 1

    def counts(self) -> tuple[int, int]:
        with self.cv:
            return self.accepted, len(self.pending)

    def submit(self, path, track: dict, cleanup: bool = True):
        """Отдать скачанный трек в анализ. cleanup — удалить файл, когда он больше не нужен."""
        label = f"{track['artist']} — {track['title']}"
        with self.cv:
            self._n += 1
            key = self._n
            self.pending[key] = label
        self.status()
        p = dict(self.p, lang=i18n.LANG)              # язык сообщений — текущий язык интерфейса
        fut = self.pool.submit(cut_track, str(path), p, self.sess.common_bpm(), self.lenient, self.pool.tmp)
        fut.add_done_callback(lambda f: self.q.put((key, f, Path(path), track, label, cleanup)))

    def _save_loop(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            key, fut, path, track, label, cleanup = item
            try:
                self._handle(fut, path, track, label)
            except Exception as e:
                self.log(T("✕ {label}: не сохранилось ({e})", label=label, e=str(e)[:200]))
                traceback.print_exc()
            finally:
                if cleanup:
                    path.unlink(missing_ok=True)
                with self.cv:
                    self.pending.pop(key, None)
                    self.cv.notify_all()
                self.status()

    def _handle(self, fut, path: Path, track: dict, label: str):
        if self.state.stop or fut.cancelled():
            return
        try:
            res = fut.result()
        except workers.BrokenProcessPool:
            self.log(T("✕ {label}: процесс анализа завершился аварийно (возможно, не хватило памяти) — пропускаю", label=label))
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
                return                            # уже набрали нужное (на всякий случай)
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
        if net is not None:
            self.net = net
        with self.cv:
            names = [n.split(" — ", 1)[-1][:28] for n in self.pending.values()]
            acc, busy = self.accepted, len(self.pending)
        parts = [self.net] if self.net else []
        if names:
            parts.append(T("анализ: {names}", names=", ".join(names)))
        with self.state.lock:
            self.state.current = " · ".join(parts) or T("Работаю…")
            self.state.progress = min(0.999, (acc + 0.5 * busy) / max(1, self.total))

    def finish(self):
        """Дождаться начатых анализов (по «Стоп» — бросить их) и закрыть процессы."""
        self.status(net="")
        deadline = None
        while True:
            if self.state.stop:
                self.pool.kill()
                deadline = deadline or time.time() + 5     # дать прибитым задачам отметиться
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
    """Копаем на YouTube Music: сеть — строго по очереди в этом потоке, анализ — в процессах."""
    p = normalize_params(params)
    sess = Session(root, state, p, p["tags"])
    log = state.log
    log(T("🎛 Теги: {tags}; {years}; {bpm}; {n} пластинок по {k} лупа, {bars} такт(а)",
          tags=p["tags"] or T("нет, случайные пластинки"),
          years=T("годы {a}–{b}", a=p["y0"], b=p["y1"]) if p["year_filter"] else T("годы любые"),
          bpm=f"BPM {p['bpm_min']}–{p['bpm_max']}" if p["bpm_filter"] else T("BPM любой"),
          n=p["n_tracks"], k=p["n_loops"], bars=p["bars"]))
    hist = History(root)
    rng = random.Random()
    try:
        fetcher = Fetcher(root / ".tmp", log, p["cookies_browser"], cookie_file=APP_DIR / "cookies.txt",
                          stop=lambda: state.stop)
    except ImportError:
        raise RuntimeError(T("Не установлен yt-dlp. Запустите программу через start-файл — он всё поставит."))

    n = p["n_tracks"]
    pool: list[dict] = []
    tried: set[str] = set()
    attempts = queries = empty_streak = 0
    max_attempts, max_queries = n * 12, n * 10
    if p["only"]:                         # изолированные куски редки — копаем глубже
        max_attempts, max_queries = n * 25, n * 20
    if p["rare"]:
        max_queries += n * 6
    rare_limit, popular_streak = 150_000, 0
    if p["rare"]:
        log(T("💎 Ищу редкие: сначала пластинки с наименьшим числом прослушиваний (до 150 тыс.)"))
    if p["only"]:
        log(T("🎯 Ищу: {what}. Сначала беру «чистые» куски, потом «с примесью»; таких мест мало — копать придётся дольше",
              what=what_ru(p)))
    pipe = Pipeline(sess, state, n, on_accept=lambda t: hist.add(t["id"]))
    net_stopped = False                   # 429 или бот-проверка: сеть стоп, скачанное дорабатываем

    try:
        while True:
            if state.stop:
                log(T("⏹ Остановлено"))
                break
            acc, busy = pipe.counts()
            if acc >= n:
                break
            # без забегания вперёд: не скачиваем больше, чем может понадобиться, и больше, чем успеем разобрать
            if net_stopped or acc + busy >= n or busy >= pipe.slots:
                if net_stopped and not busy:
                    break
                pipe.status(net="")
                pipe.wait(0.5)
                continue
            if attempts >= max_attempts:
                log(T("😕 Перебрал слишком много пластинок — попробуйте другие теги или ослабьте фильтры."))
                break
            if not pool:
                if queries >= max_queries or empty_streak >= 6:
                    log(T("😕 Поиск больше ничего не находит. Попробуйте другие теги."))
                    break
                yr = (p["y0"], p["y1"]) if p["year_filter"] else (None, None)
                q = build_query(p["tags"], *yr, rng, only=p["only"], rare=p["rare"])
                queries += 1
                pipe.status(net=T("Ищу: «{q}»", q=q))
                res = fetcher.search(q)
                fresh = [r for r in res if r["id"] not in hist.seen and r["id"] not in tried and not basic_filter(r)]
                log(T("🔎 «{q}»: найдено {n}, новых подходящих {k}", q=q, n=len(res), k=len(fresh)))
                if not fresh:
                    empty_streak += 1
                    continue
                empty_streak = 0
                rng.shuffle(fresh)            # копаем вглубь выдачи, а не только первые строчки
                if p["rare"]:
                    # сначала самые малопрослушиваемые; без счётчика — дальние строчки выдачи
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
                pipe.status(net=T("Смотрю: {label}", label=label))
                info = fetcher.probe(cand)
                title = info.get("track") or cand.get("title") or info.get("title") or "Untitled"
                artist = (info.get("artist") or cand.get("artist") or info.get("creator") or
                          (info.get("uploader") or "").replace(" - Topic", "") or "Unknown")
                if len(artist) > 60:
                    artist = artist.split(",")[0].strip()
                dur = info.get("duration") or cand.get("duration") or 0
                label = f"{artist} — {title}"
                if dur and (dur < 95 or dur > 600):
                    log(T("⏭ {label}: длина {sec} с — пропускаю", label=label, sec=int(dur)))
                    continue
                views = info.get("view_count")
                if views is None:
                    views = cand.get("views")
                if p["rare"] and views is not None and views > rare_limit:
                    log(T("⏭ {label}: слишком известная ({views} прослушиваний)", label=label, views=human_count(views)))
                    popular_streak += 1
                    if popular_streak >= 6 and rare_limit < 3_000_000:
                        rare_limit *= 2
                        popular_streak = 0
                        log(T("   (редкое тут не находится — поднимаю планку до {n})", n=human_count(rare_limit)))
                    continue
                popular_streak = 0
                if p["year_filter"]:
                    ok, year, why = era_check(cand, info, p["y0"], p["y1"], p["strict_year"])
                else:                                   # годы не фильтруем — год только для подписи
                    ok, year, why = True, era_check(cand, info, 1900, 2100, False)[1], ""
                if not ok:
                    log(T("⏭ {label}: не та эпоха ({why})", label=label, why=why))
                    continue
                pipe.status(net=T("Скачиваю: {label}", label=label))
                extra = [str(year)] if year else []
                if views is not None:
                    extra.append(T("{views} прослушиваний", views=human_count(views)))
                log(f"⬇ {label}" + (f" ({', '.join(extra)})" if extra else ""))
                path = fetcher.download(cand, info)
                if state.stop:
                    continue
                track = dict(id=cand["id"], title=title, artist=artist,
                             album=info.get("album") or cand.get("album") or "", year=year,
                             url=f"https://music.youtube.com/watch?v={cand['id']}", query=cand.get("query", ""),
                             thumb=info.get("thumbnail") or "", views=views)
                pipe.submit(path, track)                # файл удалит конвейер после анализа
                path = None
            except Stopped:                             # «Остановить» посреди запроса — не ждём ответа
                log(T("⏹ Остановлено"))
                break
            except BotBlocked as e:
                log(f"⛔ {e}")
                log(T("   Сеть остановлена, чтобы не перебирать пластинки впустую; уже скачанное дорабатываю."))
                net_stopped = True
            except Exception as e:
                msg = explain_error(e)
                log(f"✕ {label}: {msg}")
                if "429" in msg:
                    log(T("   Сеть остановлена, чтобы YouTube не ограничил сильнее; уже скачанное дорабатываю."))
                    net_stopped = True
                if not isinstance(e, RuntimeError):
                    traceback.print_exc()
            finally:
                if path:
                    Path(path).unlink(missing_ok=True)
                if fetcher.found_browser:
                    with state.lock:
                        state.found_cookies = fetcher.found_browser
    except Stopped:                         # «Остановить» во время поиска — не ждём ответа
        log(T("⏹ Остановлено"))
    pipe.finish()
    return sess.finish(pipe.accepted)


def run_local(files: list[tuple[Path, str]], params: dict, root: Path, state: JobState):
    """Режем свои файлы (рипы с винила и т.п.) — параллельно. files = [(путь, исходное имя)]"""
    p = normalize_params(params)
    sess = Session(root, state, p, "my-files")
    pipe = Pipeline(sess, state, len(files), lenient=True)
    for i, (path, orig) in enumerate(files):
        if state.stop:
            Path(path).unlink(missing_ok=True)
            continue
        stem = Path(orig).stem
        artist, title = (stem.split(" - ", 1) if " - " in stem else ("", stem))
        track = dict(id=f"local-{i}-{slug(stem, 30)}", title=title.strip() or stem, artist=artist.strip() or T("Мой файл"),
                     album="", year=None, url="", query="", thumb="", local=True)
        pipe.submit(path, track)
    pipe.finish()
    if state.stop:
        state.log(T("⏹ Остановлено"))
    return sess.finish(pipe.accepted)


def find_source(dig_dir: Path, track: dict) -> Path | None:
    """Исходный трек в папке ящика (у старых ящиков пути в dig.json нет — ищем по имени)."""
    cands = [dig_dir / track["src"]] if track.get("src") else []
    cands.append(dig_dir / SRC_DIR / (safe_name(f"{track.get('artist')} - {track.get('title')}") + ".mp3"))
    root = dig_dir.resolve()
    for c in cands:
        c = c.resolve()
        if root in c.parents and c.is_file():
            return c
    return None


def run_rerun(dig_dir: Path, old: dict, params: dict, root: Path, state: JobState):
    """Повторный прогон уже найденной пластинки с новыми настройками (питчинг/стретч, тип и длина лупа…)."""
    p = normalize_params(params)
    track = {k: old.get(k) for k in ("id", "title", "artist", "album", "year", "url", "query", "thumb", "views")}
    if old.get("local"):
        track["local"] = True
    label = f"{track['artist']} — {track['title']}"
    sess = Session(root, state, p, "rerun " + (track["title"] or "track"))
    log = state.log
    log(T("🔁 Прогоняю заново: {label} — {what}, {bars} такт(а), до {n} лупов",
          label=label, what=what_ru(p), bars=p["bars"], n=p["n_loops"]))
    pipe = Pipeline(sess, state, 1, lenient=True)
    try:
        src, downloaded = find_source(dig_dir, old), False
        if src is None:
            if old.get("local") or not old.get("url"):
                raise RuntimeError(T("исходный файл не сохранился — перетащите его в окно ещё раз"))
            log(T("   исходника нет в папке ящика — скачиваю ещё раз"))
            pipe.status(net=T("Скачиваю: {label}", label=label))
            fetcher = Fetcher(root / ".tmp", log, p["cookies_browser"], cookie_file=APP_DIR / "cookies.txt",
                              stop=lambda: state.stop)
            src, downloaded = fetcher.download({"id": old["id"]}), True
            if fetcher.found_browser:
                with state.lock:
                    state.found_cookies = fetcher.found_browser
        if not state.stop:
            pipe.submit(src, track, cleanup=downloaded)   # исходник из ящика не удаляем
    except Stopped:
        log(T("⏹ Остановлено"))
    except Exception as e:
        log(f"✕ {label}: {explain_error(e)}")
    pipe.finish()
    ok = pipe.accepted > 0
    if not ok and not state.stop:
        with state.lock:
            state.note = T("С этими настройками лупы не нашлись — подробности в журнале")
    return sess.finish(int(ok))


def write_meta(dig_dir: Path, meta: dict):
    (dig_dir / "dig.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf8")
    lines = [T("CrateDigger — откуда взяты лупы (пригодится для клиринга семплов)"), ""]
    for t in meta["tracks"]:
        lines.append(f"{t['artist']} — {t['title']}" + (f" ({t['year']})" if t.get("year") else ""))
        lines.append(f"  {t['url']}")
        for l in t["loops"]:
            a, b = l["src_start"], l["src_end"]
            how = (T("исходный темп {bpm}, растяжение {pct:+.1f}%", bpm=l["real_bpm"], pct=l["stretch_pct"])
                   if l.get("real_bpm") else T("без сетки, вырезано ровно под темп"))
            kind = (f", {T(iso.NAME_RU[l['kind']])}{T(' (с примесью)') if l.get('mixed') else ''}"
                    if l.get("kind") else "")
            lines.append(f"  {l['name']}: {int(a // 60)}:{a % 60:05.2f}–{int(b // 60)}:{b % 60:05.2f}, "
                         + T("{bars} такт(а)", bars=f"{l['bars']:g}") + f", {how}{kind}")
        lines.append("")
    (dig_dir / "credits.txt").write_text("\n".join(lines), "utf8")
    params = meta.get("params") or {}
    mono = params.get("formats") in ("both", "mono")
    ms = [T("Как играть эти лупы на Elektron Model:Samples"), "",
          (T("1. Перекиньте файлы из папки ModelSamples_mono16 через Elektron Transfer.") if mono else
           T("1. Перекиньте файлы из папки Stereo_24bit через Elektron Transfer (он сам переведёт в моно).")),
          T("2. Темп паттерна = число в имени файла (Gmaj90Name1 -> 90 BPM)."),
          T("3. Длина трека (Track Length) = количество шагов из таблицы ниже, триг на шаге 1."),
          T("   Луп перезапускается каждый цикл, поэтому никогда не «уплывает»."),
          T("4. Чопы: лупы ровно по тактам, поэтому ручка START делит их на равные части:"),
          T("   0, 16, 32, 48, 64, 80, 96, 112 ≈ восьмые части лупа. Ставьте разные START на"),
          T("   разные шаги (параметр-локи) — получится перенарезка, как на MPC."),
          T("5. Лупы с одинаковой цифрой Camelot или соседней (8A, 8B, 7A, 9A) звучат вместе."),
          T("6. Если шагов больше 64 — поставьте у трека SCALE 1/2X и длину вдвое меньше."),
          *([T("7. Файлы разложены по папкам: ModelSamples_mono16/<тональность>/<BPM>/ —"),
             T("   перекиньте папку целиком, на приборе найдёте нужное по тональности и темпу.")]
            if mono and params.get("sort_ms") else []), "",
          f"{T('Файл'):<34}{'BPM':>5}{T('Тактов'):>9}{T('Шагов'):>7}  {T('Тональн.'):<8} Camelot"]
    for t in meta["tracks"]:
        for l in t["loops"]:
            ms.append(f"{l['name']:<34}{l['bpm']:>5}{l['bars']:>9g}{l['steps']:>7}  {l['key']:<8} {l['camelot']}")
    (dig_dir / "MODEL_SAMPLES.txt").write_text("\n".join(ms), "utf8")
