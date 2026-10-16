"""
Звуковой движок CrateDigger: только numpy + scipy + ffmpeg.

Что умеет:
  * декодировать любой аудиофайл через ffmpeg
  * находить темп и сетку долей (beat tracking, динамическое программирование)
  * находить сильную долю такта (downbeat)
  * определять тональность (хрома + профили Крумхансла, с поправкой на строй пластинки)
  * выбирать самые «закольцовываемые» куски трека
  * растягивать луп до темпа, кратного 5 BPM, и резать его с точностью до сэмпла
"""
from __future__ import annotations

import functools
import math
import os
import shutil
import subprocess
import uuid
import wave
from fractions import Fraction

import numpy as np
from scipy import ndimage, signal

from . import iso, spec
from .spec import stft_mag
from .i18n import T

# «Только барабаны» — луп должен пройти все проверки сразу:
DRUM_PITCHED_MAX = 0.035  # кадров с гармоническим рядом (бас, клавиши, голос, гитара, духовые) — почти ноль
DRUM_PITCHED_RUN = 1      # и ни одной серии таких кадров подряд (ловит короткие стабы)
DRUM_GAP_DB = 15.0        # громкость между ударами проваливается минимум на столько дБ (в басу и серединах)
DRUM_TONAL_MAX = 0.30     # и общая «нотность» спектра низкая
SR = 48000            # рабочая частота (Model:Samples тоже 48 кГц)
ASR = 24000           # частота для анализа
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# --------------------------------------------------------------------------- ffmpeg
@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg  # ставит готовый ffmpeg вместе с pip
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    if p:
        return p
    raise RuntimeError(T("ffmpeg не найден (pip install imageio-ffmpeg)"))


def _ff(args, data: bytes | None = None) -> bytes:
    r = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", *args],
        input=data, capture_output=True, creationflags=_NOWIN,
    )
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf8", "replace")[-400:])
    return r.stdout


I16 = 16384.0        # исходник храним в int16 на −6 дБ: настоящий сэмпл = q / I16 (без клиппинга)


def load_audio_i16(path, sr: int = SR, tmp_dir=None) -> np.ndarray:
    """Любой файл -> int16 стерео (N, 2), сэмпл = q / I16 (уровень −6 дБ — без клиппинга; 15 бит
    точнее mp3/YouTube-исходника). Вдвое меньше float32.
    tmp_dir — ffmpeg пишет звук во временный файл, а массив отображается на него (memmap):
    звук не занимает личную память процесса, система сама подгружает нужные куски.
    Файл удаляется release_audio()."""
    args = ["-i", str(path), "-vn", "-af", "volume=0.5", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "2", "-ar", str(sr)]
    if tmp_dir is None:
        x = np.frombuffer(_ff(args + ["pipe:1"]), "<i2").reshape(-1, 2)
    else:
        os.makedirs(tmp_dir, exist_ok=True)
        f = os.path.join(str(tmp_dir), f"an_{os.getpid()}_{uuid.uuid4().hex[:8]}.s16")
        try:
            _ff(["-y"] + args + [f])
            x = np.memmap(f, dtype="<i2", mode="r").reshape(-1, 2) if os.path.getsize(f) >= 4 else np.zeros((0, 2), np.int16)
        except Exception:
            _unlink(f)
            raise
    if len(x) < sr * 5:
        release_audio(x)
        raise RuntimeError(T("слишком короткий или пустой файл"))
    return x


def release_audio(x) -> None:
    """Закрыть отображение и удалить временный файл звука (если он был)."""
    mm = x if isinstance(x, np.memmap) else getattr(x, "base", None)
    while mm is not None and not isinstance(mm, np.memmap):
        mm = getattr(mm, "base", None)
    if isinstance(mm, np.memmap):
        f = mm.filename
        try:
            mm._mmap.close()                 # на Windows файл нельзя удалить, пока он отображён
        except Exception:
            pass
        _unlink(f)


def _unlink(f):
    try:
        os.remove(f)
    except OSError:
        pass


def save_mp3(src_path, dst_path, bitrate="320k"):
    _ff(["-y", "-i", str(src_path), "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate, str(dst_path)])


def write_wav(path, x: np.ndarray, sr: int = SR, bits: int = 24):
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    if bits == 16:
        rng = np.random.default_rng(0)
        dither = (rng.random(x.shape) - rng.random(x.shape)) / 32768.0   # TPDF
        q = np.round(np.clip(x + dither, -1, 1) * 32767).astype("<i2")
        data = q.tobytes()
    else:
        q = np.round(x * 8388607).astype("<i4")
        data = np.ascontiguousarray(q).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(bits // 8)
        w.setframerate(sr)
        w.writeframes(data)


# --------------------------------------------------------------------------- спектр
def mel_filterbank(sr, n_fft, n_mels=64, fmin=30.0, fmax=11000.0):
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    fb = np.zeros((n_mels, len(freqs)), np.float32)
    for i in range(n_mels):
        l, c, r = pts[i:i + 3]
        up = (freqs - l) / max(c - l, 1e-9)
        down = (r - freqs) / max(r - c, 1e-9)
        fb[i] = np.maximum(0.0, np.minimum(up, down))
        if fb[i].sum() == 0:                       # слишком узкий фильтр на низах
            fb[i, int(np.argmin(np.abs(freqs - c)))] = 1.0
    return fb, pts[1:-1]


# --------------------------------------------------------------------------- тональность
# Профили Крумхансла–Кесслера
_KK_MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
# Профили Темперли (лучше на поп/соул/джазе)
_TP_MAJ = np.array([5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0])
_TP_MIN = np.array([5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0])


def _z(v):
    v = np.asarray(v, float)
    return (v - v.mean()) / (v.std() + 1e-12)


_PROF_MAJ = _z(_z(_KK_MAJ) + _z(_TP_MAJ))
_PROF_MIN = _z(_z(_KK_MIN) + _z(_TP_MIN))


def key_scores(ch: np.ndarray) -> np.ndarray:
    """Корреляция с 24 тональностями: [0..11] мажор, [12..23] минор."""
    c = _z(ch)
    out = np.zeros(24)
    for t in range(12):
        out[t] = np.dot(c, np.roll(_PROF_MAJ, t)) / 12.0
        out[12 + t] = np.dot(c, np.roll(_PROF_MIN, t)) / 12.0
    return out


def key_from_scores(sc: np.ndarray):
    order = np.argsort(sc)[::-1]
    best, second = sc[order[0]], sc[order[1]]
    k = int(order[0])
    conf = max(0.0, min(1.0, (best - second) * 4 + best * 0.5))
    return k % 12, ("maj" if k < 12 else "min"), conf


def key_name(tonic: int, mode: str) -> str:
    return f"{NOTE_NAMES[tonic]}{mode}"


def camelot(tonic: int, mode: str) -> str:
    rel_major = tonic if mode == "maj" else (tonic + 3) % 12
    num = ((rel_major * 7) % 12 + 7) % 12 + 1
    return f"{num}{'B' if mode == 'maj' else 'A'}"


def chroma_matrix(freqs: np.ndarray, tuning_cents: float, fmin=65.406, fmax=2093.0):
    """Матрица бинов спектра -> 12 классов высоты (с учётом строя)."""
    M = np.zeros((12, len(freqs)), np.float32)
    k = 2 ** (tuning_cents / 1200.0)          # ровно 5 октав C2..C7, чтобы ни одна нота не перевешивала
    ok = (freqs >= fmin * k * 2 ** (-1 / 24)) & (freqs < fmax * k * 2 ** (-1 / 24))
    midi = 69 + 12 * np.log2(np.maximum(freqs, 1e-6) / 440.0) - tuning_cents / 100.0
    nearest = np.round(midi)
    dist = midi - nearest
    w = np.exp(-0.5 * (dist / 0.22) ** 2) * ok
    pcs = (nearest.astype(int)) % 12
    for pc in range(12):
        M[pc] = np.where(pcs == pc, w, 0.0)
    return M


def tonal_peaks(S: np.ndarray) -> np.ndarray:
    """Оставляем только тональные пики спектра (ноты), убираем шум, шипение винила и барабаны."""
    lm = ndimage.uniform_filter1d(S, 31, axis=1, mode="nearest")
    a = np.roll(S, 1, axis=1)
    b = np.roll(S, -1, axis=1)
    pk = (S >= a) & (S >= b) & (S > 2.0 * lm)
    out = np.where(pk, S - lm, 0.0).astype(np.float32)
    # пик «размазан» на соседние бины — учитываем их тоже
    return out + 0.5 * (np.roll(out, 1, axis=1) + np.roll(out, -1, axis=1))


def tonal_chroma(S: np.ndarray, freqs: np.ndarray, tuning_cents: float) -> np.ndarray:
    """-> (кадры, 12), каждый кадр нормирован; плюс веса кадров"""
    M = chroma_matrix(freqs, tuning_cents).T
    return np.concatenate([(tonal_peaks(S[i:i + 2048]) ** 1.5) @ M for i in range(0, max(len(S), 1), 2048)])


def estimate_tuning(S: np.ndarray, freqs: np.ndarray) -> float:
    """Насколько пластинка «уплыла» от A=440 (в центах, -50..+50). Старые записи часто не в строе."""
    sub = S[:: max(1, len(S) // 600)]
    band = (freqs > 100) & (freqs < 2000)
    idx = np.where(band)[0]
    if len(idx) < 5 or len(sub) == 0:
        return 0.0
    X = sub[:, idx[0] - 1: idx[-1] + 2]
    a, b, c = X[:, :-2], X[:, 1:-1], X[:, 2:]
    peak = (b > a) & (b > c) & (b > np.percentile(b, 90, axis=1, keepdims=True))
    denom = a - 2 * b + c
    off = np.where(np.abs(denom) > 1e-12, 0.5 * (a - c) / np.where(denom == 0, 1, denom), 0.0)
    df = freqs[1] - freqs[0]
    fr = (freqs[idx][None, :] + off * df)
    fr = fr[peak]
    wts = b[peak]
    if len(fr) < 20:
        return 0.0
    cents = 1200 * np.log2(fr / 440.0)
    ang = 2 * np.pi * (cents % 100) / 100.0
    t = math.atan2(float((wts * np.sin(ang)).sum()), float((wts * np.cos(ang)).sum()))
    cents_off = t / (2 * np.pi) * 100.0
    return float((cents_off + 50) % 100 - 50)


def wrap_tuning(cents: float):
    """Строй + сдвиг -> (строй в -50..50, на сколько полутонов сдвинулись названия нот)."""
    k = math.floor((cents + 50) / 100.0)
    return cents - 100 * k, k


def loop_chroma(x48: np.ndarray, tuning_cents: float) -> tuple[np.ndarray, float]:
    """Хрома короткого лупа. -> (вектор 12, «ясность» гармонии 0..1)"""
    y = signal.resample_poly(x48.mean(1), 1, 2).astype(np.float32)
    n_fft, hop = 4096, 1024
    S = stft_mag(y, n_fft, hop, max_bin=400)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / ASR)[:400]
    C = tonal_chroma(spec.hpss(S, 9, 13)[0], freqs, tuning_cents)
    norm = np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
    w = np.sqrt(norm[:, 0])
    ch = ((C / norm) * w[:, None]).sum(0)
    ch = ch / (ch.sum() + 1e-9)
    ent = -float(np.sum(ch * np.log(ch + 1e-12))) / math.log(12)
    clarity = float(np.clip((1 - ent) * 4, 0, 1))
    return ch, clarity


def harmonic_frames(C: np.ndarray, df: float, min_harm: int = 5, step: int = 1500) -> np.ndarray:
    """Для каждого кадра спектра: есть ли звук с гармоническим рядом (≥ min_harm обертонов
    из первых восьми на местах f, 2f, 3f…). У барабанов обертоны негармоничны — их нет."""
    nb = C.shape[1]
    f = np.arange(nb) * df
    f0s = np.geomspace(70, 700, 140)
    K = np.arange(1, 9)
    bins = np.round(np.outer(f0s, K) / df).astype(int)
    valid = bins < nb
    bins = np.minimum(bins, nb - 1)
    out = np.zeros(len(C), bool)
    for i in range(0, len(C), step):
        X = C[i:i + step]
        lm = ndimage.uniform_filter1d(X, 31, axis=1, mode="nearest")
        pk = (X >= np.roll(X, 1, 1)) & (X >= np.roll(X, -1, 1)) & (X > 2.5 * lm) & (f[None, :] > 50)
        pkd = pk | np.roll(pk, 1, 1) | np.roll(pk, -1, 1)
        P = pkd[:, bins] & valid[None]                   # (кадры, кандидаты f0, гармоники)
        cnt = np.where(P[:, :, 0] | P[:, :, 1], P.sum(2), 0)
        out[i:i + step] = cnt.max(1) >= min_harm
    return out


def longest_run(b: np.ndarray) -> int:
    best = cur = 0
    for v in b:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def attack_curve(hp: np.ndarray) -> np.ndarray:
    """Кривая «подъёма громкости» с точностью до сэмпла.
    Огибающая = максимум за последние 12 мс (так низкая бочка не «дрожит»)."""
    W = int(0.012 * SR)
    env = ndimage.maximum_filter1d(np.abs(hp), W, origin=(W - 1) // 2)
    env = ndimage.uniform_filter1d(env, 12)
    rise = np.zeros_like(env)
    rise[24:] = env[24:] - env[:-24]
    return rise


# --------------------------------------------------------------------------- анализ трека
class TrackAnalysis:
    """Весь анализ одного трека."""

    def __init__(self, audio: np.ndarray, want_iso: bool = False):
        """audio — int16 из load_audio_i16 (или float32 (N, 2)). want_iso — сразу посчитать
        признаки для «только бас / вокал / пэды» (иначе они не нужны и память не тратится)."""
        if audio.dtype != np.int16:
            audio = np.clip(np.round(np.asarray(audio, np.float32) * I16), -32768, 32767).astype(np.int16)
        self.x = audio                   # сам звук (int16, сэмпл = q / I16) — нужен только для нарезки
        self.dur = len(audio) / SR
        y, self.rise, self.rms_total = self._stream(audio)

        # --- онсеты (атаки) для темпа и сетки: mel-полосы сразу по кускам, без полного спектра
        n_fft, hop = 1024, 256
        self.fps = ASR / hop
        fb, centers = mel_filterbank(ASR, n_fft, 64, 30, 11000)
        fbT = fb.T.astype(np.float32)
        L = spec.db(spec.stft_reduce(y, n_fft, hop, lambda X: (X * X) @ fbT))
        L = np.maximum(L, L.max() - 80)
        self.mel = L.astype(np.float32)
        Ep = 10 ** (self.mel / 10.0)
        self.band_low_db = spec.db(Ep[:, (centers > 40) & (centers < 250)].sum(1)).astype(np.float32)
        self.band_mid_db = spec.db(Ep[:, (centers > 300) & (centers < 3000)].sum(1)).astype(np.float32)
        del Ep
        flux = np.maximum(0.0, np.diff(L, axis=0, prepend=L[:1]))
        low = centers < 200
        self.onset = self._norm_onset(flux.mean(1))
        self.onset_low = self._norm_onset(flux[:, low].mean(1))
        del flux, L
        # признаки «только бас / вокал / пэды» — сейчас, пока есть сигнал 24 кГц (долю «нотных» кадров
        # допишем ниже, когда посчитаем длинный спектр)
        self._iso = iso.features(y, ASR, self.mel, centers, self.fps, None, ASR / 1024) if want_iso else None

        # --- спектр для гармонии (длинное окно)
        cn, ch = 4096, 1024
        self.cfps = ASR / ch
        maxb = int(5000 / (ASR / cn))
        C = stft_mag(y, cn, ch, max_bin=maxb)
        del y                                                     # дальше сигнал 24 кГц не нужен
        cfreqs = np.fft.rfftfreq(cn, 1.0 / ASR)[:maxb]
        # доля «нот» в спектре 55–2100 Гц: у чистых барабанов ~0.1, с басом или аккордами 0.45+
        tb = (cfreqs >= 55) & (cfreqs <= 2100)
        tf = []
        for i in range(0, len(C), 2048):
            Cb = C[i:i + 2048, tb]
            tf.append((tonal_peaks(Cb) ** 2).sum(1) / ((Cb * Cb).sum(1) + 1e-12))
        self.tonal_frac = np.concatenate(tf).astype(np.float32)
        # кадры, где звучит что-то с гармоническим рядом f, 2f, 3f… (любой инструмент или голос)
        self.pitched = harmonic_frames(C, cfreqs[1] - cfreqs[0])
        if self._iso is not None:
            self._iso["pitched"] = self.pitched.astype(bool)
        eh, ep = spec.hpss_inplace(C, 17, 17)                     # C -> тянущаяся часть, на месте
        self.harm_ratio = (eh / (eh + ep + 1e-12)).astype(np.float32)
        self.tuning = estimate_tuning(C, cfreqs)
        chroma = tonal_chroma(C, cfreqs, self.tuning)
        del C
        self.chroma_raw = chroma.astype(np.float32)
        nrm = np.linalg.norm(chroma, axis=1, keepdims=True) + 1e-9
        self.chroma = (chroma / nrm).astype(np.float32)
        tch = (self.chroma * np.sqrt(nrm)).sum(0)
        self.track_chroma = tch / (tch.sum() + 1e-9)

        # --- темп и сетка
        self.tempo = self.estimate_tempo()
        self.period = self.fps * 60.0 / self.tempo
        bf = self.track_beats(self.onset, self.period)
        self.beat_frames = bf
        self.beat_times, self.beat_strength = self.refine_times(bf / self.fps)
        ibi = np.diff(self.beat_times)
        med = float(np.median(ibi)) if len(ibi) else 1.0
        self.ibi_cv = float(np.std(ibi[(ibi > med * 0.7) & (ibi < med * 1.3)]) / med) if len(ibi) > 4 else 1.0
        self.rms_db = 20 * math.log10(self.rms_total)
        loc = ndimage.gaussian_filter1d(self.onset.astype(np.float64), 2)
        bfi = np.clip(bf.astype(int), 0, len(loc) - 1)
        self.pulse = float(loc[bfi].mean() / (loc.mean() + 1e-9)) if len(bfi) else 0.0
        oo = self.onset - self.onset.mean()
        nn = len(oo)
        Fo = np.fft.rfft(oo, 1 << int(np.ceil(np.log2(2 * nn))))
        aco = np.fft.irfft(Fo * np.conj(Fo))[:nn]
        aco = aco / (aco[0] + 1e-9)
        li = int(self.period)
        self.pulse_ac = float(max(aco[min(li, nn - 1)], aco[min(li + 1, nn - 1)], aco[min(2 * li, nn - 1)]))
        self.downbeat_phase = self.find_downbeat_phase()
        self.beat_feats = self.beat_features()

    @staticmethod
    def _stream(x: np.ndarray):
        """Кусками по 10 с: моно 24 кГц, кривая атак 48 кГц и общая громкость.
        Полная моно-копия трека и 64-битные временные массивы не создаются."""
        n = len(x)
        k = np.float32(0.5 / I16)

        def mono(a, b):
            return (x[a:b, 0].astype(np.float32) + x[a:b, 1]) * k

        y = spec.down_stream(mono, n, 2)                          # 24 кГц для анализа
        sos = signal.butter(2, 70, "highpass", fs=SR, output="sos").astype(np.float32)
        zi = np.zeros((sos.shape[0], 2), np.float32)
        rise = np.empty(n, np.float16)                            # кривая атак: точности float16 хватает
        R, LA = 2048, 64                                          # контекст слева (окно 12 мс) и справа
        hist, h0, done, ss = np.zeros(0, np.float32), 0, 0, 0.0
        for a in range(0, n, spec.BLOCK):
            m = mono(a, a + spec.BLOCK)
            ss += float(np.dot(m.astype(np.float64), m))
            hp, zi = signal.sosfilt(sos, m, zi=zi)
            hist = np.concatenate([hist, hp])
            r = attack_curve(hist)
            end = h0 + len(hist) if a + len(m) >= n else h0 + len(hist) - LA
            rise[done:end] = r[done - h0:end - h0]
            done = end
            keep = max(0, done - R)
            hist, h0 = hist[keep - h0:], keep
        return y, rise, float(np.sqrt(ss / max(n, 1)) + 1e-9)

    def audio(self, a: int, b: int) -> np.ndarray:
        """Кусок звука [a, b) в сэмплах -> float32 (N, 2)."""
        a, b = max(0, int(a)), min(len(self.x), int(b))
        return self.x[a:b].astype(np.float32) * np.float32(1.0 / I16)

    @property
    def iso(self) -> dict:
        """Признаки для «только бас / вокал / пэды» (TrackAnalysis(..., want_iso=True))."""
        if self._iso is None:
            raise RuntimeError("признаки изоляции не посчитаны: нужен TrackAnalysis(..., want_iso=True)")
        return self._iso

    def drum_metrics(self, t0: float, t1: float) -> dict:
        """Проверка «только барабаны» для куска t0..t1 (в секундах)."""
        ca, cb = int(t0 * self.cfps), max(int(t1 * self.cfps), int(t0 * self.cfps) + 2)
        pf = self.pitched[ca:cb]
        frac = float(pf.mean()) if len(pf) else 1.0
        run = longest_run(pf)
        ma, mb = int(t0 * self.fps), max(int(t1 * self.fps), int(t0 * self.fps) + 4)
        lo, mi = self.band_low_db[ma:mb], self.band_mid_db[ma:mb]
        low_gap = float(np.percentile(lo, 95) - np.percentile(lo, 15)) if len(lo) else 0.0
        mid_gap = float(np.percentile(mi, 95) - np.percentile(mi, 15)) if len(mi) else 0.0
        tonal = float(self.tonal_frac[ca:cb].mean()) if cb > ca else 1.0
        ok = (frac <= DRUM_PITCHED_MAX and run <= DRUM_PITCHED_RUN and low_gap >= DRUM_GAP_DB
              and mid_gap >= DRUM_GAP_DB and tonal <= DRUM_TONAL_MAX)
        return dict(ok=ok, pitched=frac, run=run, low_gap=low_gap, mid_gap=mid_gap, tonal=tonal)

    def rhythm_problem(self) -> str | None:
        """Почему этот трек не годится для ровных лупов (или None, если всё хорошо)."""
        if self.rms_db < -45:
            return T("почти тишина")
        if len(self.beat_times) < 24:
            return T("не нашёл ритма")
        if self.pulse < 2.5 or self.pulse_ac < 0.15:
            return T("нет чёткого ритма (шум, эмбиент или пэды)")
        if self.ibi_cv > 0.05:
            return T("темп «плавает» (рубато, живой темп без метронома)")
        return None

    # ----------------------------------------------------------------- helpers
    def _norm_onset(self, o):
        o = o - ndimage.uniform_filter1d(o, int(self.fps * 0.4))
        o = np.maximum(o, 0)
        o = ndimage.gaussian_filter1d(o, 1.0)
        return (o / (o.std() + 1e-9)).astype(np.float32)

    def estimate_tempo(self) -> float:
        """Темп, «свёрнутый» в диапазон 57.5..122.5 BPM (все октавы складываются вместе)."""
        o = self.onset + 0.5 * self.onset_low
        o = o - o.mean()
        n = len(o)
        nfft = 1 << int(np.ceil(np.log2(2 * n)))
        F = np.fft.rfft(o, nfft)
        ac = np.fft.irfft(F * np.conj(F))[:n]
        ac = ac / (ac[0] + 1e-9)

        def acv(lag):
            lag = np.asarray(lag, float)
            i = np.clip(lag.astype(int), 0, n - 2)
            f = lag - i
            return ac[i] * (1 - f) + ac[i + 1] * f

        T = np.arange(57.5, 122.5, 0.05)
        beat_lag = self.fps * 60.0 / T
        score = (1.0 * acv(beat_lag) + 0.6 * acv(beat_lag / 2) + 0.3 * acv(beat_lag / 4)
                 + 0.6 * acv(beat_lag * 2) + 0.4 * acv(beat_lag * 4))
        prior = np.exp(-0.5 * (np.log2(T / 92.0) / 0.6) ** 2)
        score = score * (1 + 0.15 * prior)
        return float(T[int(np.argmax(score))])

    @staticmethod
    def track_beats(onset, period, tightness=120.0):
        """Beat tracking (Эллис, 2007) — динамическое программирование."""
        n = len(onset)
        sig = max(period / 32.0, 0.5)
        local = ndimage.gaussian_filter1d(onset.astype(np.float64), sig)
        lo, hi = int(round(period * 0.5)), int(round(period * 2.0))
        offs = np.arange(-hi, -lo + 1)
        txwt = -tightness * (np.log(-offs / period)) ** 2
        score = np.zeros(n)
        back = -np.ones(n, dtype=np.int64)
        for t in range(n):
            z = t + offs
            m = z >= 0
            if m.any():
                cand = score[z[m]] + txwt[m]
                k = int(np.argmax(cand))
                if cand[k] > 0:
                    score[t] = local[t] + cand[k]
                    back[t] = z[m][k]
                    continue
            score[t] = local[t]
        # последний удар
        mx = signal.argrelmax(score, order=max(1, int(period / 4)))[0]
        if len(mx) == 0:
            return np.array([], float)
        thr = 0.5 * np.median(score[mx])
        last = int(mx[score[mx] > thr][-1]) if (score[mx] > thr).any() else int(mx[-1])
        beats = []
        t = last
        while t >= 0:
            beats.append(t)
            t = back[t]
        beats = np.array(beats[::-1], float)
        # отбрасываем «пустые» доли в начале/конце (тишина, затухание)
        strong = local[beats.astype(int)] > 0.25 * np.median(local[beats.astype(int)])
        if strong.any():
            a, b = np.argmax(strong), len(strong) - np.argmax(strong[::-1])
            beats = beats[a:b]
        return beats

    def refine_times(self, times, win=0.03):
        """Подтягиваем каждую долю к реальной атаке с точностью до сэмпла."""
        rise = self.rise
        out, st = [], []
        w, ctx = int(win * SR), int(0.25 * SR)
        for t in times:
            c = int(t * SR)
            a, b = max(0, c - w), min(len(rise), c + w)
            if b - a < 4:
                out.append(t); st.append(0.0); continue
            seg = rise[a:b]
            k = int(np.argmax(seg))
            ca, cb = max(0, c - ctx), min(len(rise), c + ctx)
            ref = float(np.percentile(np.abs(rise[ca:cb]), 95)) + 1e-9
            s = float(seg[k]) / ref
            # самое начало атаки: первая точка перед пиком, где подъём > 25% пика
            lo = max(0, k - int(0.02 * SR))
            below = np.where(seg[lo:k + 1] <= 0.25 * seg[k])[0]
            k0 = lo + int(below[-1]) + 1 if len(below) else lo
            out.append((a + k0) / SR if s > 0.6 else t)
            st.append(s)
        return np.array(out), np.array(st)

    def find_downbeat_phase(self) -> int:
        bf = self.beat_frames.astype(int)
        if len(bf) < 8:
            return 0
        ol = self.onset_low[np.clip(bf, 0, len(self.onset_low) - 1)]
        of = self.onset[np.clip(bf, 0, len(self.onset) - 1)]
        # смена гармонии на сильную долю
        hc = np.zeros(len(bf))
        cf = np.clip((bf / self.fps * self.cfps).astype(int), 1, len(self.chroma) - 1)
        hc = 1 - np.sum(self.chroma[cf] * self.chroma[cf - 1], axis=1)
        best, bs = 0, -1e9
        for ph in range(4):
            sc = (1.0 * ol[ph::4].mean() + 0.4 * of[ph::4].mean() + 0.8 * hc[ph::4].mean() * of.mean()
                  - 0.3 * ol[(ph + 1) % 4::4].mean())
            if sc > bs:
                best, bs = ph, sc
        return best

    def beat_features(self):
        """Тембр + гармония на каждую долю — чтобы сравнивать куски между собой."""
        bt = self.beat_times
        nb = len(bt) - 1
        if nb < 2:
            return np.zeros((0, 76)), np.zeros(0), np.zeros(0), np.zeros(0)
        mel_i = np.clip((bt * self.fps).astype(int), 0, len(self.mel) - 1)
        ch_i = np.clip((bt * self.cfps).astype(int), 0, len(self.chroma) - 1)
        timb = np.zeros((nb, self.mel.shape[1]))
        chro = np.zeros((nb, 12))
        harm = np.zeros(nb)
        rms = np.zeros(nb)
        for i in range(nb):
            a, b = mel_i[i], max(mel_i[i + 1], mel_i[i] + 1)
            timb[i] = self.mel[a:b].mean(0)
            ca, cb = ch_i[i], max(ch_i[i + 1], ch_i[i] + 1)
            chro[i] = self.chroma[ca:cb].mean(0)
            harm[i] = self.harm_ratio[ca:cb].mean()
            sa, sb = int(bt[i] * SR), int(bt[i + 1] * SR)
            m = self.x[sa:sb].astype(np.float32).sum(1) * np.float32(0.5 / I16)
            rms[i] = np.sqrt(np.mean(m * m) + 1e-12)
        self.timb_stats = (timb.mean(0), timb.std(0) + 1e-6)
        timb = (timb - self.timb_stats[0]) / self.timb_stats[1]
        timb /= (np.linalg.norm(timb, axis=1, keepdims=True) + 1e-9)
        chro /= (np.linalg.norm(chro, axis=1, keepdims=True) + 1e-9)
        feats = np.hstack([0.75 * timb, 0.65 * chro])
        feats /= (np.linalg.norm(feats, axis=1, keepdims=True) + 1e-9)
        return feats, harm, rms, chro

    # ----------------------------------------------------------------- подбор лупов
    def candidates(self, bars=2, prefer="any", step_beats=4, min_energy=0.35):
        feats, harm, rms, chro = self.beat_feats
        L = bars * 4
        nb = len(feats)
        bt = self.beat_times
        if nb < L + 2:
            return []
        med_rms = float(np.median(rms)) + 1e-9
        med_ibi = float(np.median(np.diff(bt)))
        out = []
        start0 = self.downbeat_phase
        for s in range(start0, nb - L + 1, step_beats):
            e = s + L
            if e >= len(bt):
                break
            if bt[s] < (0.15 if prefer == "drums" else 1.0) or bt[e] > self.dur - 3.0:
                continue
            if prefer == "drums":
                dm = self.drum_metrics(bt[s], bt[e])
                if not dm["ok"]:
                    continue      # слышны бас, аккорды, голос или стабы — это не чистые барабаны
            ibi = np.diff(bt[s:e + 1])
            if (ibi < med_ibi * 0.8).any() or (ibi > med_ibi * 1.25).any():
                continue
            stab = 1 - min(1.0, float(np.std(ibi) / np.mean(ibi)) * 12)
            reps = []
            if e + L <= nb:
                reps.append(float(np.mean(np.sum(feats[s:e] * feats[e:e + L], 1))))
            if s - L >= 0:
                reps.append(float(np.mean(np.sum(feats[s - L:s] * feats[s:e], 1))))
            rep = max(reps) if reps else 0.3
            seam = 0.7 * float(np.dot(feats[s], feats[e])) if e < nb else 0.3
            seam += 0.3 * float(np.dot(feats[s - 1], feats[e - 1])) if s > 0 else 0.15
            energy = float(np.mean(rms[s:e]) / med_rms)
            if energy < min_energy:
                continue
            onset_ok = min(1.0, float(self.beat_strength[s]) / 2.0)
            h = float(np.mean(harm[s:e]))
            ch = chro[s:e].mean(0)
            clarity = float(np.clip((ch.max() - ch.mean()) / (ch.mean() + 1e-9) / 2, 0, 1))
            if prefer == "breaks":
                bonus = (1 - h) * 1.2 - clarity * 0.3
            elif prefer == "drums":
                bonus = (0.5 * min(1.0, (dm["low_gap"] - DRUM_GAP_DB) / 15) + 0.5 * min(1.0, (dm["mid_gap"] - DRUM_GAP_DB) / 15)
                         + 0.4 * (1 - dm["pitched"] / DRUM_PITCHED_MAX) + 0.3 * (1 - h))
            elif prefer == "melodic":
                bonus = h * 0.8 + clarity * 0.6
            else:
                bonus = 0.25
            score = (0.32 * rep + 0.22 * seam + 0.14 * stab + 0.12 * min(1.0, energy)
                     + 0.08 * onset_ok + 0.12 * bonus)
            out.append(dict(s=s, e=e, t0=float(bt[s]), t1=float(bt[e]), score=score, rep=rep, seam=seam,
                            stab=stab, energy=energy, harm=h, sig=feats[s:e].mean(0)))
        return out

    def pick_loops(self, n=3, bars=2, prefer="any", rng=None):
        cands = self.candidates(bars, prefer)
        for c in cands:
            c["rank"] = c["score"]
        return select_loops(cands, n, rng, (0.93, 0.975, 0.985) if prefer == "drums" else (0.93, 0.975, 1.01))

    # ----------------------------------------------------------------- изолированные партии
    def iso_metrics(self, t0: float, t1: float) -> dict:
        """Метрики изоляции; короткие куски оцениваем вместе с соседством (минимум 4 с)."""
        pad = max(0.0, (4.0 - (t1 - t0)) / 2)
        return iso.metrics(self.iso, max(0.0, t0 - pad), min(self.dur, t1 + pad))

    def sig_at(self, t0: float, t1: float) -> np.ndarray:
        """«Подпись» куска вне сетки — в том же пространстве, что и у кандидатов по долям."""
        a, b = int(t0 * self.fps), max(int(t1 * self.fps), int(t0 * self.fps) + 1)
        timb = self.mel[a:b].mean(0)
        mu, sd = getattr(self, "timb_stats", (self.mel.mean(0), self.mel.std(0) + 1e-6))
        timb = (timb - mu) / sd
        timb /= np.linalg.norm(timb) + 1e-9
        ca, cb = int(t0 * self.cfps), max(int(t1 * self.cfps), int(t0 * self.cfps) + 1)
        ch = self.chroma[ca:cb].mean(0)
        ch /= np.linalg.norm(ch) + 1e-9
        f = np.hstack([0.75 * timb, 0.65 * ch])
        return f / (np.linalg.norm(f) + 1e-9)

    def iso_candidates(self, kind: str, bars: int, free_bpm: float | None = None) -> list[dict]:
        """Куски, где звучит только `kind`. По сетке долей, а для пэдов и вокала — ещё и там,
        где сетки нет (вступление без бита, эмбиент): такие режем ровно под free_bpm."""
        if kind == "drums":
            out = self.candidates(bars, "drums")
            for c in out:
                c.update(kind=kind, grade=2, rank=c["score"])
            return out
        out = []
        grid_ok = self.rhythm_problem() is None
        if grid_ok:
            for c in self.candidates(bars, "any", min_energy=0.08):     # пэды и вступления тише припева
                m = self.iso_metrics(c["t0"], c["t1"])
                g = iso.grade(m, kind)
                if g:
                    c.update(kind=kind, grade=g, rank=0.6 * iso.score(m, kind) + 0.4 * c["score"])
                    out.append(c)
        if kind in ("pads", "vocal") and free_bpm:
            L = bars * 4 * 60.0 / free_bpm
            bt = self.beat_times
            spans = [(0.5, self.dur - 1.0)] if not grid_ok or len(bt) < 2 else \
                [(0.5, float(bt[0])), (float(bt[-1]), self.dur - 1.0)]
            F = self.iso
            floor = float(np.median(F["tot"])) - 25          # не берём почти тишину
            for a, b in spans:
                t = a
                while t + L <= b:
                    m = self.iso_metrics(t, t + L)
                    g = iso.grade(m, kind)
                    fa, fb = int(t * F["fps"]), int((t + L) * F["fps"])
                    if g and float(np.median(F["tot"][fa:fb])) > floor:
                        out.append(dict(free=True, t0=t, t1=t + L, kind=kind, grade=g,
                                        rank=0.6 * iso.score(m, kind) + 0.2, sig=self.sig_at(t, t + L)))
                    t += L / 2
        return out


def select_loops(cands: list[dict], n: int, rng=None, sims=(0.93, 0.975, 1.01)) -> list[dict]:
    """Лучшие n непересекающихся и непохожих друг на друга кусков.
    Порядок: сначала «чисто» (grade 2), потом «с примесью»; внутри — по порядку типов, затем по рангу."""
    if not cands:
        return []
    rng = rng or np.random.default_rng()
    order = {k: i for i, k in enumerate(iso.KINDS)}
    for c in cands:   # немного случайности, как в настоящем копании
        c["_r"] = c["rank"] + float(rng.normal(0, 0.012))
    cands = sorted(cands, key=lambda c: (-c.get("grade", 2), order.get(c.get("kind"), 0), -c["_r"]))
    chosen = []
    for thresh in sims:
        for c in cands:
            if len(chosen) >= n:
                break
            if any(c is d for d in chosen):
                continue
            if any(not (c["t1"] <= d["t0"] or c["t0"] >= d["t1"]) for d in chosen):
                continue
            sig = c["sig"] / (np.linalg.norm(c["sig"]) + 1e-9)
            if any(float(np.dot(sig, d["sig"] / (np.linalg.norm(d["sig"]) + 1e-9))) > thresh for d in chosen):
                continue
            chosen.append(c)
        if len(chosen) >= n:
            break
    return chosen          # в порядке приоритета (лучшие первыми)


# --------------------------------------------------------------------------- темп и рендер
def choose_target(real_bpm, bpm_min=60, bpm_max=120, fixed_bpm=None, max_stretch=0.05):
    """Подбираем целевой темп (кратный 5). Можно взять трек вдвое быстрее/медленнее.
    -> (target_bpm, множитель интерпретации, ratio) либо None"""
    best = None
    for mult in (1.0, 2.0, 0.5):
        x = real_bpm * mult
        if fixed_bpm:
            targets = [float(fixed_bpm)]
        else:
            t = 5 * round(x / 5.0)
            targets = [t, t - 5, t + 5]
        for t in targets:
            if not (bpm_min <= t <= bpm_max):
                continue
            ratio = t / x
            dev = abs(math.log(ratio))
            pen = dev + (0.0 if mult == 1.0 else 0.012)
            if dev <= math.log(1 + max_stretch) + 1e-9 and (best is None or pen < best[3]):
                best = (int(t), mult, ratio, pen)
    if best is None:
        return None
    return best[0], best[1], best[2]


def varispeed(seg: np.ndarray, ratio: float) -> tuple[np.ndarray, float]:
    """Питчинг (как на вертушке): быстрее = выше. -> (аудио, точный фактический ratio)"""
    fr = Fraction(1.0 / ratio).limit_denominator(1200)
    up, down = fr.numerator, fr.denominator
    z = signal.resample_poly(seg, up, down, axis=0).astype(np.float32)
    return z, down / up


# --------------------------------------------------------------------------- свой тайм-стретч
def _wsola(x: np.ndarray, out_len: int, keep: int, N: int = 1536, tol: int = 512) -> np.ndarray:
    """WSOLA с картой времени: первые `keep` сэмплов идут 1:1 (атака не трогается),
    остальное равномерно сжимается/растягивается до out_len."""
    L = len(x)
    if out_len <= 0:
        return np.zeros((0, x.shape[1]), np.float32)
    keep = min(keep, L, out_len)
    if L - keep < N // 2 or out_len - keep < N // 2:      # кусок слишком короткий — просто ресемплим хвост
        tail = x[keep:]
        need = out_len - keep
        if need <= 0:
            return x[:out_len].copy()
        if len(tail) < 2:
            return np.vstack([x[:keep], np.zeros((need, x.shape[1]), np.float32)])
        pos = np.linspace(0, len(tail) - 1, need)
        res = np.stack([np.interp(pos, np.arange(len(tail)), tail[:, c]) for c in range(x.shape[1])], 1)
        return np.vstack([x[:keep], res]).astype(np.float32)
    Hs = N // 2
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / N)).astype(np.float32)   # периодический Ханн: сумма = 1
    pad = N
    xp = np.vstack([np.zeros((pad, x.shape[1]), np.float32), x, np.zeros((pad, x.shape[1]), np.float32)])
    xm = xp.mean(1)
    y = np.zeros((out_len + 2 * N, x.shape[1]), np.float32)
    ws = np.zeros(out_len + 2 * N, np.float32)
    slope = (L - keep) / (out_len - keep)

    def nominal(t):            # t — позиция в выходе (без паддинга)
        return t if t < keep else keep + (t - keep) * slope

    prev = None
    k = 0
    while True:
        t_out = k * Hs - pad          # левый край кадра в координатах выхода
        if t_out >= out_len:
            break
        c = t_out + N // 2            # центр кадра
        nom = int(round(nominal(c) - N // 2)) + pad
        if prev is None or c < keep:
            pos = nom
        else:
            nat = prev + Hs
            lo = max(0, nom - tol)
            hi = min(len(xp) - N, nom + tol)
            if hi <= lo or nat + N > len(xp):
                pos = min(max(nom, 0), len(xp) - N)
            else:
                tmpl = xm[nat:nat + N]
                corr = np.correlate(xm[lo:hi + N], tmpl, mode="valid")
                # берём только настоящие пики (не край окна), ближние к расчётной позиции — чуть выгоднее
                inner = np.where((corr[1:-1] > corr[:-2]) & (corr[1:-1] >= corr[2:]))[0] + 1
                if len(inner):
                    lags = (lo + inner - nom) / float(tol)
                    sc = corr[inner] * (1.0 - 0.35 * lags ** 2)
                    pos = lo + int(inner[int(np.argmax(sc))])
                else:
                    pos = nom
        pos = min(max(pos, 0), len(xp) - N)
        o = k * Hs
        y[o:o + N] += xp[pos:pos + N] * win[:, None]
        ws[o:o + N] += win
        prev = pos
        k += 1
    y = y[pad:pad + out_len]
    w = ws[pad:pad + out_len]
    return (y / np.maximum(w, 1e-3)[:, None]).astype(np.float32)


def find_anchors(an: "TrackAnalysis", a: int, b: int, min_gap=0.07) -> list[int]:
    """Атаки (удары) внутри [a, b) — в сэмплах трека."""
    r = an.rise[a:b]
    if len(r) < SR // 10:
        return []
    hop = SR // 1000
    n = len(r) // hop
    rm = r[: n * hop].reshape(n, hop).max(1)
    peaks, _ = signal.find_peaks(rm, height=0.22 * float(rm.max() + 1e-12), distance=int(min_gap * 1000))
    out = []
    for pk in peaks:
        c = pk * hop
        lo = max(0, c - int(0.02 * SR))
        seg = r[lo:c + hop]
        if not len(seg):
            continue
        k = int(np.argmax(seg))
        below = np.where(seg[:k + 1] <= 0.25 * seg[k])[0]
        k0 = lo + (int(below[-1]) + 1 if len(below) else 0)
        out.append(a + k0)
    return out


def anchored_stretch(x: np.ndarray, P0: float, P1: float, anchors: list[int], ratio: float,
                     out_len: int) -> np.ndarray:
    """Растягивает x[P0:P1] в out_len сэмплов. Каждая атака попадает ровно в (A-P0)/ratio,
    сама атака не растягивается; растягиваются только «хвосты» между ударами."""
    F = int(0.008 * SR)                      # перекрёстное затухание на стыке кусков
    K = int(0.03 * SR)                       # сколько атаки оставлять нетронутой
    P0i = int(round(P0))
    pts = [P0i] + [A for A in sorted(anchors) if P0i + 0.06 * SR < A < P1 - 0.06 * SR]
    clean = [pts[0]]
    for A in pts[1:]:
        if A - clean[-1] >= 0.06 * SR:
            clean.append(A)
    pts = clean + [int(round(P1))]
    pos = [int(round((A - P0) / ratio)) for A in pts]
    pos[-1] = out_len
    out = np.zeros((out_len + 2 * F, x.shape[1]), np.float32)
    fade = np.linspace(0, 1, F, dtype=np.float32)[:, None]
    for i in range(len(pts) - 1):
        ia, ib = pts[i] - F, pts[i + 1]
        if ia < 0:
            ia = 0
        lead = pts[i] - ia
        piece_in = x[ia:ib]
        T = pos[i + 1] - pos[i] + lead
        piece = _wsola(piece_in, T, keep=lead + K)
        if len(piece) < T:
            piece = np.vstack([piece, np.zeros((T - len(piece), x.shape[1]), np.float32)])
        o = F + pos[i] - lead
        if i > 0 and lead > 0:
            piece = piece.copy()
            piece[:lead] *= fade[-lead:] if lead < F else fade
            out[o:o + lead] *= (1 - fade[-lead:]) if lead < F else (1 - fade)
        out[o:o + T] += piece
    return out[F:F + out_len]


def _finish(loop: np.ndarray, pre: np.ndarray | None, normalize: bool) -> np.ndarray:
    """Стык лупа. pre — звук сразу перед началом лупа: конец лупа плавно переходит в него,
    и луп становится бесшовным (для пэдов и вокала — тянущийся звук не щёлкает).
    Без pre — короткие микрофейды, как для ударных (атака на первой доле не размывается)."""
    loop = loop.copy()
    if pre is not None and len(pre) >= 64:
        xf = len(pre)
        t = np.linspace(0, np.pi / 2, xf, dtype=np.float32)[:, None]
        loop[-xf:] = loop[-xf:] * np.cos(t) + pre * np.sin(t)       # равная мощность
    else:
        fi, fo = int(0.0008 * SR), int(0.004 * SR)
        loop[:fi] *= np.linspace(0, 1, fi)[:, None]
        loop[-fo:] *= np.linspace(1, 0, fo)[:, None] ** 2
    if normalize:
        rms = float(np.sqrt(np.mean(loop ** 2)) + 1e-9)
        peak = float(np.max(np.abs(loop)) + 1e-9)
        loop *= min(10 ** (-16 / 20) / rms, 10 ** (-0.5 / 20) / peak)
    return loop


XFADE = {"pads": 0.25, "vocal": 0.03}      # длина бесшовного стыка, с (не больше 1/8 лупа)


SONG_KEY_KINDS = ("drums", "bass", "vocal")   # одна линия или одни удары — лад не определить, берём песню


def _loop_key(an: TrackAnalysis, loop: np.ndarray, t0: float, t1: float, tuning_out: float,
              kind: str | None = None) -> dict:
    """Тональность лупа: голосуют сам луп и окрестность ±20 с в треке.
    Для изолированных барабанов, баса и вокала — тональность всей песни."""
    tun, shift = wrap_tuning(tuning_out)
    if kind in SONG_KEY_KINDS:
        ts = key_scores(np.roll(an.track_chroma, shift) if shift else an.track_chroma)
        tonic, kmode, conf = key_from_scores(ts)
        te = an.chroma_raw.sum(1)
        ca, cb = int(t0 * an.cfps), max(int(t1 * an.cfps), int(t0 * an.cfps) + 1)
        return dict(key=key_name(tonic, kmode), camelot=camelot(tonic, kmode), key_conf=round(conf, 2),
                    tuning_cents=round(tun, 1), tonal=round(float(np.mean(te[ca:cb]) / (np.median(te) + 1e-9)), 2))
    lch, clarity = loop_chroma(loop, tun)
    # контекст: ±20 секунд вокруг лупа (так переживаем модуляции внутри песни)
    fa, fb = max(0, int((t0 - 20) * an.cfps)), min(len(an.chroma), int((t1 + 20) * an.cfps) + 1)
    w = np.sqrt(np.linalg.norm(an.chroma_raw[fa:fb], axis=1))
    ctx = (an.chroma[fa:fb] * w[:, None]).sum(0)
    ctx = ctx / (ctx.sum() + 1e-9) if ctx.sum() > 0 else an.track_chroma
    tch = np.roll(ctx, shift) if shift else ctx
    # луп внутри песни обычно в её тональности, но если кусок явно в другой (бридж) — победит луп
    ls, ts = key_scores(lch), key_scores(tch)
    tk = int(np.argmax(ts))
    # сколько в куске «нот» относительно всего трека (у барабанного брейка почти ноль)
    te = an.chroma_raw.sum(1)
    ca, cb = int(t0 * an.cfps), max(int(t1 * an.cfps), int(t0 * an.cfps) + 1)
    tonal_rel = float(np.mean(te[ca:cb]) / (np.median(te) + 1e-9))
    if tonal_rel < 0.35 or ls[tk] >= ls.max() - 0.25:
        # почти одни барабаны или луп не противоречит песне -> тональность песни
        tonic, kmode = tk % 12, ("maj" if tk < 12 else "min")
        conf = float(np.clip(0.5 * (ts[tk] + ls[tk]), 0, 1))
    else:                                  # явная модуляция — верим лупу
        wl = 0.45 + 0.35 * clarity
        tonic, kmode, conf = key_from_scores(wl * ls + (1 - wl) * ts)
    return dict(key=key_name(tonic, kmode), camelot=camelot(tonic, kmode), key_conf=round(conf, 2),
                tuning_cents=round(tun, 1), tonal=round(tonal_rel, 2))


_HP70 = signal.butter(2, 70, "highpass", fs=SR, output="sos").astype(np.float32)


def onset_start(an: TrackAnalysis, t: float) -> float:
    """Настоящее начало удара у доли t. От пика удара идём назад до места, где громкость впервые
    заметно поднялась над тишиной перед ним (мягкая атака, флэм) — не дальше 30 мс и не позже t."""
    B = 24                                                    # шаг 0.5 мс
    a = int((t - 0.2) * SR)
    if a < 0:
        return t
    m = signal.sosfilt(_HP70, an.audio(a, (t + 0.025) * SR).sum(1))
    n = len(m) // B
    if n < 440:
        return t
    blk = np.abs(m[:n * B]).reshape(n, B).max(1)
    env = np.maximum(blk[:-1], blk[1:])                       # пик за 1 мс с шагом 0.5 мс
    ti = int(round((t * SR - a) / B))
    floor = float(np.median(env[ti - 120:ti - 50])) + 1e-9    # тишина 60–25 мс перед долей
    w0 = ti - 10
    pk = w0 + int(np.argmax(env[w0:ti + 30]))                 # сам удар: −5…+15 мс
    peak = float(env[pk])
    if peak < 2.5 * floor:
        return t                                              # нет явного удара — доверяем сетке
    thr = max(2.0 * floor, 0.1 * peak)
    j, lo = pk, ti - 60
    while j > lo and env[j - 1] >= thr:
        j -= 1
    return min(t, (a + j * B) / SR)


def tail_leak(loop: np.ndarray) -> int:
    """Сколько последних сэмплов лупа — уже начало следующего удара («щелчок» на стыке). 0 — стык чистый."""
    m = np.abs(loop).max(1) if loop.ndim == 2 else np.abs(loop)
    B = SR // 1000
    n = len(m) // B
    if n < 150:
        return 0
    e = 20 * np.log10(m[:n * B].reshape(n, B).max(1) + 1e-9)
    body = float(np.median(e[-120:-20]))
    hot = np.nonzero(e[-15:-2] > body + 9)[0]                 # последние 15 мс (без самого фейда)
    return 0 if not len(hot) else len(m) - (n - 15 + int(hot[0])) * B


def _fade_tail(loop: np.ndarray, n: int) -> np.ndarray:
    n = min(len(loop) // 4, n)
    loop = loop.copy()
    loop[-n:] *= np.cos(np.linspace(0, np.pi / 2, n, dtype=np.float32))[:, None]
    return loop


def render_loop(an: TrackAnalysis, s: int, e: int, opts: dict) -> dict | None:
    """Вырезает луп s..e (индексы долей), подгоняет темп, режет ровно по сетке.
    Границы — по настоящему началу ударов; после нарезки проверяется стык: если в конец попало
    начало следующего удара, конец сдвигается раньше и луп режется заново (в «Питчинге» — с чуть
    большим замедлением). Не вышло за 2 раза: барабаны не берём, остальное — мягкий фейд в конце.
    opts: mode, bpm_min/max, fixed_bpm, max_stretch, normalize, kind."""
    bt = an.beat_times
    t0, t1 = onset_start(an, float(bt[s])), onset_start(an, float(bt[e]))
    r, seam = None, "ok"
    for attempt in range(3):
        nr = _render_span(an, t0, t1, e - s, opts)
        if nr is None:
            break
        r = nr
        leak = tail_leak(r["audio"])
        if not leak:
            r["seam"] = seam
            return r
        seam = "shifted"
        t1 -= leak * (t1 - t0) / len(r["audio"]) + 0.002     # длина утечки в секундах исходника
    if r is None:
        return None
    if opts.get("kind") == "drums":
        return None                                           # чистые брейки: лучше взять другой кусок
    r["audio"] = _fade_tail(r["audio"], tail_leak(r["audio"]) + SR // 500)
    r["seam"] = "fade"
    return r


def _render_span(an: TrackAnalysis, t0: float, t1: float, beats: int, opts: dict) -> dict | None:
    real_bpm = 60.0 * beats / (t1 - t0)
    pick = choose_target(real_bpm, opts.get("bpm_min", 60), opts.get("bpm_max", 120),
                         opts.get("fixed_bpm"), opts.get("max_stretch", 0.05))
    if pick is None:
        return None
    target, mult, ratio = pick
    named_beats = beats * mult
    out_len = int(round(named_beats * 60.0 / target * SR))
    xf = min(int(XFADE.get(opts.get("kind"), 0) * SR), out_len // 8)

    pre = 0.003                                    # чуть до атаки, чтобы не съесть её начало
    P0, P1 = (t0 - pre) * SR, (t1 - pre) * SR      # луп в сэмплах трека
    tuning_out = an.tuning
    if opts.get("mode") == "vinyl":                # питчинг: темп меняется вместе с высотой
        a = max(0, int(P0 - 0.3 * SR))
        z, real_ratio = varispeed(an.audio(a, P1 + 0.6 * SR), ratio)
        i0 = int(round((P0 - a) / real_ratio))
        loop = z[i0:i0 + out_len]
        head = z[max(0, i0 - xf):i0]
        tuning_out += 1200 * math.log2(real_ratio)
        engine = "pitch"
    else:                                          # стретч: удары стоят на местах, высота та же
        base = max(0, int(P0) - int(0.1 * SR))                 # только нужный кусок трека, не весь
        seg = an.audio(base, P1 + 0.1 * SR)
        anchors = [A - base for A in find_anchors(an, int(P0), int(P1))]
        loop = anchored_stretch(seg, P0 - base, P1 - base, anchors, (P1 - P0) / out_len, out_len)
        head = an.audio(int(P0) - xf, int(P0)) if xf else None
        engine = "stretch"
    if len(loop) < out_len:
        loop = np.vstack([loop, np.zeros((out_len - len(loop), 2), np.float32)])
    loop = _finish(loop, head if xf and head is not None and len(head) == xf else None, opts.get("normalize", True))

    return dict(audio=loop.astype(np.float32), **_loop_key(an, loop, t0, t1, tuning_out, opts.get("kind")),
                bpm=target, real_bpm=round(real_bpm * mult, 2),
                stretch_pct=round((ratio - 1) * 100, 2), pitch_st=round(12 * math.log2(ratio), 2) if engine == "pitch" else 0,
                bars=named_beats / 4.0, beats=named_beats,
                engine=engine, src_start=round(t0, 3), src_end=round(t1, 3),
                drums=bool(an.drum_metrics(t0, t1)["ok"]))


def render_free(an: TrackAnalysis, t0: float, bars: float, target: int, opts: dict) -> dict:
    """Кусок вне сетки (пэды, вокал без бита): ровно bars тактов при target BPM, без растяжения
    и без смены высоты — тянущемуся звуку темп не важен, важна длина. Стык бесшовный."""
    out_len = int(round(bars * 4 * 60.0 / target * SR))
    xf = min(int(XFADE.get(opts.get("kind"), 0.1) * SR), out_len // 8)
    P0 = max(xf, int(t0 * SR))
    loop = an.audio(P0, P0 + out_len)
    if len(loop) < out_len:
        loop = np.vstack([loop, np.zeros((out_len - len(loop), 2), np.float32)])
    loop = _finish(loop, an.audio(P0 - xf, P0), opts.get("normalize", True))
    leak = tail_leak(loop)
    if leak:                                                   # у тянущегося звука — мягкий фейд
        loop = _fade_tail(loop, leak + SR // 500)
    t1 = (P0 + out_len) / SR
    return dict(audio=loop.astype(np.float32), **_loop_key(an, loop, P0 / SR, t1, an.tuning, opts.get("kind")),
                bpm=int(target), real_bpm=None, stretch_pct=0.0, pitch_st=0, bars=float(bars), beats=bars * 4,
                engine="cut", free=True, src_start=round(P0 / SR, 3), src_end=round(t1, 3), drums=False)


def waveform_peaks(x: np.ndarray, n=240):
    m = np.abs(x).max(1) if x.ndim == 2 else np.abs(x)
    k = max(1, len(m) // n)
    p = m[: k * n].reshape(-1, k).max(1)
    return [round(float(v), 3) for v in p / (p.max() + 1e-9)]
