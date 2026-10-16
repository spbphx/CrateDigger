"""
Sound engine of CrateDigger: only numpy + scipy + ffmpeg.

What it does:
  * decodes any audio file through ffmpeg
  * finds the tempo and the beat grid (beat tracking, dynamic programming)
  * finds the downbeat (the first beat of the bar)
  * detects the key (chroma + Krumhansl profiles, corrected for the tuning of the record)
  * picks the most "loopable" spans of the track
  * fits a loop to a tempo that is a multiple of 5 BPM and cuts it with sample accuracy

Time units: t / t0 / t1 are seconds in the source track, s / e are beat indices in beat_times,
P0 / P1 / a / b are sample positions at SR (48 kHz).
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

# "Drums only" — a loop must pass all of these checks at once:
DRUM_PITCHED_MAX = 0.035  # frames with a harmonic series (bass, keys, voice, guitar, horns) — almost none
DRUM_PITCHED_RUN = 1      # and not a single run of such frames in a row (catches short stabs)
DRUM_GAP_DB = 15.0        # loudness between hits drops by at least this many dB (in the lows and mids)
DRUM_TONAL_MAX = 0.30     # and the overall "noteness" of the spectrum is low
SR = 48000            # working sample rate (the Model:Samples uses 48 kHz too)
ASR = 24000           # sample rate used for analysis
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# --------------------------------------------------------------------------- ffmpeg
@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """Path of the ffmpeg binary (the one bundled with imageio-ffmpeg, else the system one)."""
    try:
        import imageio_ffmpeg  # installs a ready-made ffmpeg together with pip
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    if p:
        return p
    raise RuntimeError(T("ffmpeg not found (pip install imageio-ffmpeg)"))


def _ff(args, data: bytes | None = None) -> bytes:
    """Run ffmpeg with the given arguments (optionally feeding bytes to stdin). -> stdout bytes"""
    r = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", *args],
        input=data, capture_output=True, creationflags=_NOWIN,
    )
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf8", "replace")[-400:])
    return r.stdout


I16 = 16384.0        # the source is stored as int16 at -6 dB: real sample = q / I16 (no clipping)


def load_audio_i16(path, sr: int = SR, tmp_dir=None) -> np.ndarray:
    """Any file -> int16 stereo (N, 2), sample = q / I16 (level -6 dB — no clipping; 15 bits are
    more precise than an mp3/YouTube source). Half the size of float32.
    tmp_dir — ffmpeg writes the audio to a temporary file and the array is mapped onto it (memmap):
    the audio doesn't take the process's private memory, the OS loads the needed pieces itself.
    The file is deleted by release_audio()."""
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
        raise RuntimeError(T("the file is too short or empty"))
    return x


def release_audio(x) -> None:
    """Close the memory mapping and delete the temporary audio file (if there was one)."""
    mm = x if isinstance(x, np.memmap) else getattr(x, "base", None)
    while mm is not None and not isinstance(mm, np.memmap):
        mm = getattr(mm, "base", None)
    if isinstance(mm, np.memmap):
        f = mm.filename
        try:
            mm._mmap.close()                 # on Windows a file can't be deleted while it is mapped
        except Exception:
            pass
        _unlink(f)


def _unlink(f):
    try:
        os.remove(f)
    except OSError:
        pass


def save_mp3(src_path, dst_path, bitrate="320k"):
    """Encode any audio file to a 320 kbps mp3 (used to keep the source track next to the loops)."""
    _ff(["-y", "-i", str(src_path), "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate, str(dst_path)])


def write_wav(path, x: np.ndarray, sr: int = SR, bits: int = 24):
    """Write a WAV file: 24-bit, or 16-bit with TPDF dither (for the Model:Samples mono files)."""
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


# --------------------------------------------------------------------------- spectrum
def mel_filterbank(sr, n_fft, n_mels=64, fmin=30.0, fmax=11000.0):
    """Triangular mel filters. -> (filters (n_mels, bins), centre frequencies in Hz)"""
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
        if fb[i].sum() == 0:                       # filter too narrow in the lows
            fb[i, int(np.argmin(np.abs(freqs - c)))] = 1.0
    return fb, pts[1:-1]


# --------------------------------------------------------------------------- key detection
# Krumhansl-Kessler key profiles
_KK_MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
# Temperley profiles (better on pop/soul/jazz); both are averaged
_TP_MAJ = np.array([5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0])
_TP_MIN = np.array([5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0])


def _z(v):
    v = np.asarray(v, float)
    return (v - v.mean()) / (v.std() + 1e-12)


_PROF_MAJ = _z(_z(_KK_MAJ) + _z(_TP_MAJ))
_PROF_MIN = _z(_z(_KK_MIN) + _z(_TP_MIN))


def key_scores(ch: np.ndarray) -> np.ndarray:
    """Correlation with the 24 keys: [0..11] major, [12..23] minor."""
    c = _z(ch)
    out = np.zeros(24)
    for t in range(12):
        out[t] = np.dot(c, np.roll(_PROF_MAJ, t)) / 12.0
        out[12 + t] = np.dot(c, np.roll(_PROF_MIN, t)) / 12.0
    return out


def key_from_scores(sc: np.ndarray):
    """Best of the 24 key scores. -> (tonic 0..11, "maj"/"min", confidence 0..1)"""
    order = np.argsort(sc)[::-1]
    best, second = sc[order[0]], sc[order[1]]
    k = int(order[0])
    conf = max(0.0, min(1.0, (best - second) * 4 + best * 0.5))
    return k % 12, ("maj" if k < 12 else "min"), conf


def key_name(tonic: int, mode: str) -> str:
    """Key name used in file names: "Amin", "C#maj"."""
    return f"{NOTE_NAMES[tonic]}{mode}"


def camelot(tonic: int, mode: str) -> str:
    """Camelot wheel code (8A, 10B...): same or neighbouring numbers mix well."""
    rel_major = tonic if mode == "maj" else (tonic + 3) % 12
    num = ((rel_major * 7) % 12 + 7) % 12 + 1
    return f"{num}{'B' if mode == 'maj' else 'A'}"


def chroma_matrix(freqs: np.ndarray, tuning_cents: float, fmin=65.406, fmax=2093.0):
    """Matrix spectrum bins -> 12 pitch classes (taking the tuning into account)."""
    M = np.zeros((12, len(freqs)), np.float32)
    k = 2 ** (tuning_cents / 1200.0)          # exactly 5 octaves C2..C7, so no single note outweighs the rest
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
    """Keep only the tonal peaks of the spectrum (notes); remove noise, vinyl hiss and drums."""
    lm = ndimage.uniform_filter1d(S, 31, axis=1, mode="nearest")
    a = np.roll(S, 1, axis=1)
    b = np.roll(S, -1, axis=1)
    pk = (S >= a) & (S >= b) & (S > 2.0 * lm)
    out = np.where(pk, S - lm, 0.0).astype(np.float32)
    # a peak is "smeared" over neighbouring bins — count them too
    return out + 0.5 * (np.roll(out, 1, axis=1) + np.roll(out, -1, axis=1))


def tonal_chroma(S: np.ndarray, freqs: np.ndarray, tuning_cents: float) -> np.ndarray:
    """-> (frames, 12), every frame normalised; plus frame weights"""
    M = chroma_matrix(freqs, tuning_cents).T
    return np.concatenate([(tonal_peaks(S[i:i + 2048]) ** 1.5) @ M for i in range(0, max(len(S), 1), 2048)])


def estimate_tuning(S: np.ndarray, freqs: np.ndarray) -> float:
    """How far the record "drifted" from A=440 (cents, -50..+50). Old recordings are often off."""
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
    """Tuning + shift -> (tuning in -50..50, by how many semitones the note names moved)."""
    k = math.floor((cents + 50) / 100.0)
    return cents - 100 * k, k


def loop_chroma(x48: np.ndarray, tuning_cents: float) -> tuple[np.ndarray, float]:
    """Chroma of a short loop. -> (12-vector, "clarity" of the harmony 0..1)"""
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
    """For every spectrum frame: is there a sound with a harmonic series (>= min_harm partials
    out of the first eight at f, 2f, 3f...). Drum partials are inharmonic — they have none."""
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
        P = pkd[:, bins] & valid[None]                   # (frames, f0 candidates, harmonics)
        cnt = np.where(P[:, :, 0] | P[:, :, 1], P.sum(2), 0)
        out[i:i + step] = cnt.max(1) >= min_harm
    return out


def longest_run(b: np.ndarray) -> int:
    """Length of the longest run of True values."""
    best = cur = 0
    for v in b:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def attack_curve(hp: np.ndarray) -> np.ndarray:
    """Sample-accurate "loudness rise" curve (used to find exact attack positions).
    Envelope = maximum over the last 12 ms (so a low kick doesn't "wobble")."""
    W = int(0.012 * SR)
    env = ndimage.maximum_filter1d(np.abs(hp), W, origin=(W - 1) // 2)
    env = ndimage.uniform_filter1d(env, 12)
    rise = np.zeros_like(env)
    rise[24:] = env[24:] - env[:-24]
    return rise


# --------------------------------------------------------------------------- track analysis
class TrackAnalysis:
    """All analysis of one track: tempo, beat grid, key, timbre per beat, isolation features."""

    def __init__(self, audio: np.ndarray, want_iso: bool = False):
        """audio — int16 from load_audio_i16 (or float32 (N, 2)). want_iso — compute the features for
        "bass / vocals / pads only" right away (otherwise they aren't needed and no memory is spent)."""
        if audio.dtype != np.int16:
            audio = np.clip(np.round(np.asarray(audio, np.float32) * I16), -32768, 32767).astype(np.int16)
        self.x = audio                   # the audio itself (int16, sample = q / I16) — only needed for cutting
        self.dur = len(audio) / SR
        y, self.rise, self.rms_total = self._stream(audio)

        # --- onsets (attacks) for tempo and grid: mel bands computed chunk by chunk, no full spectrum
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
        # "bass / vocals / pads only" features — now, while the 24 kHz signal exists (the share of
        # "note" frames is filled in below, once the long spectrum is computed)
        self._iso = iso.features(y, ASR, self.mel, centers, self.fps, None, ASR / 1024) if want_iso else None

        # --- spectrum for harmony (long window)
        cn, ch = 4096, 1024
        self.cfps = ASR / ch
        maxb = int(5000 / (ASR / cn))
        C = stft_mag(y, cn, ch, max_bin=maxb)
        del y                                                     # the 24 kHz signal is not needed any more
        cfreqs = np.fft.rfftfreq(cn, 1.0 / ASR)[:maxb]
        # share of "notes" in the 55-2100 Hz spectrum: pure drums ~0.1, with bass or chords 0.45+
        tb = (cfreqs >= 55) & (cfreqs <= 2100)
        tf = []
        for i in range(0, len(C), 2048):
            Cb = C[i:i + 2048, tb]
            tf.append((tonal_peaks(Cb) ** 2).sum(1) / ((Cb * Cb).sum(1) + 1e-12))
        self.tonal_frac = np.concatenate(tf).astype(np.float32)
        # frames where something with a harmonic series f, 2f, 3f... sounds (any instrument or voice)
        self.pitched = harmonic_frames(C, cfreqs[1] - cfreqs[0])
        if self._iso is not None:
            self._iso["pitched"] = self.pitched.astype(bool)
        eh, ep = spec.hpss_inplace(C, 17, 17)                     # C -> its sustained part, in place
        self.harm_ratio = (eh / (eh + ep + 1e-12)).astype(np.float32)
        self.tuning = estimate_tuning(C, cfreqs)
        chroma = tonal_chroma(C, cfreqs, self.tuning)
        del C
        self.chroma_raw = chroma.astype(np.float32)
        nrm = np.linalg.norm(chroma, axis=1, keepdims=True) + 1e-9
        self.chroma = (chroma / nrm).astype(np.float32)
        tch = (self.chroma * np.sqrt(nrm)).sum(0)
        self.track_chroma = tch / (tch.sum() + 1e-9)

        # --- tempo and beat grid
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
        """In 10 s chunks: mono 24 kHz, attack curve at 48 kHz and overall loudness.
        No full mono copy of the track and no 64-bit temporary arrays are created."""
        n = len(x)
        k = np.float32(0.5 / I16)

        def mono(a, b):
            return (x[a:b, 0].astype(np.float32) + x[a:b, 1]) * k

        y = spec.down_stream(mono, n, 2)                          # 24 kHz for analysis
        sos = signal.butter(2, 70, "highpass", fs=SR, output="sos").astype(np.float32)
        zi = np.zeros((sos.shape[0], 2), np.float32)
        rise = np.empty(n, np.float16)                            # attack curve: float16 precision is enough
        R, LA = 2048, 64                                          # context on the left (12 ms window) and on the right
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
        """A piece of audio [a, b) in samples -> float32 (N, 2)."""
        a, b = max(0, int(a)), min(len(self.x), int(b))
        return self.x[a:b].astype(np.float32) * np.float32(1.0 / I16)

    @property
    def iso(self) -> dict:
        """Features for "bass / vocals / pads only" (TrackAnalysis(..., want_iso=True))."""
        if self._iso is None:
            raise RuntimeError("isolation features were not computed: TrackAnalysis(..., want_iso=True) is needed")
        return self._iso

    def drum_metrics(self, t0: float, t1: float) -> dict:
        """The "drums only" check for the span t0..t1 (seconds)."""
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

    def drum_purity(self, t0: float, t1: float) -> float:
        """How "pure" a drum span is, 0..1 (shown in Debugging mode): no harmonic frames and deep
        gaps between hits give 1."""
        d = self.drum_metrics(t0, t1)
        return float(0.5 * np.clip(1 - d["pitched"] / DRUM_PITCHED_MAX, 0, 1)
                     + 0.25 * np.clip((d["low_gap"] - DRUM_GAP_DB) / 15, 0, 1)
                     + 0.25 * np.clip((d["mid_gap"] - DRUM_GAP_DB) / 15, 0, 1))

    def edge_jump(self, t0: float, t1: float) -> float:
        """Loudness jump at the wrap of a loop t0..t1, dB: the level of the first 100 ms minus the level of
        the 120 ms before the end (the last 30 ms are skipped — a new attack may begin there).
        A vocal loop that starts in the middle of a loud passage and ends quiet jumps at every restart."""
        def level(a, b):
            x = self.audio(int(a * SR), int(b * SR)).astype(np.float64)
            return 10 * np.log10(float(np.mean(x * x)) + 1e-12)
        return level(t0, t0 + 0.10) - level(t1 - 0.15, t1 - 0.03)

    def late_entry(self, t0: float, t1: float, tail: float = 0.5) -> bool:
        """Does something new come in during the last `tail` seconds of the span — a louder part, a new
        note, or a new instrument in some frequency band? For off-grid pads such a span would cut that
        sound off at every restart of the loop."""
        F = self.iso
        fps = F["fps"]
        a, b, k = int(t0 * fps), int(t1 * fps), int(tail * fps)
        if b - a < 2 * k:
            return False
        tot = F["tot"][a:b]
        if np.percentile(tot[-k:], 90) - np.median(tot[:-k]) >= 6.0:
            return True                                   # clearly louder at the end (a slow swell is fine)
        h = F["hon"][a:b]
        if h[-k:].max() >= max(2.5 * float(np.median(F["hon"])), 1.6 * float(np.percentile(h[:-k], 90))):
            return True                                   # a new note starts
        fm = self.fps
        am, bm, km = int(t0 * fm), int(t1 * fm), int(tail * fm)
        rest = 10 * np.log10((10 ** (self.mel[am:bm - km] / 10)).mean(0) + 1e-10)
        end = 10 * np.log10((10 ** (self.mel[bm - km:bm] / 10)).mean(0) + 1e-10)
        loud = rest > rest.max() - 35                     # bands that actually carry sound
        return bool(np.max((end - rest)[loud]) >= 8.0)     # a new instrument in some band

    def rhythm_problem(self) -> str | None:
        """Why this track is no good for tight loops (or None if all is well)."""
        if self.rms_db < -45:
            return T("almost silence")
        if len(self.beat_times) < 24:
            return T("no rhythm found")
        if self.pulse < 2.5 or self.pulse_ac < 0.15:
            return T("no clear rhythm (noise, ambient or pads)")
        if self.ibi_cv > 0.05:
            return T("the tempo drifts (rubato, live playing without a click)")
        return None

    # ----------------------------------------------------------------- helpers
    def _norm_onset(self, o):
        o = o - ndimage.uniform_filter1d(o, int(self.fps * 0.4))
        o = np.maximum(o, 0)
        o = ndimage.gaussian_filter1d(o, 1.0)
        return (o / (o.std() + 1e-9)).astype(np.float32)

    def estimate_tempo(self) -> float:
        """Tempo "folded" into 57.5..122.5 BPM (all octaves are added together)."""
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
        """Beat tracking (Ellis, 2007) — dynamic programming over the onset curve."""
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
        # the last beat
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
        # drop "empty" beats at the start/end (silence, fade-out)
        strong = local[beats.astype(int)] > 0.25 * np.median(local[beats.astype(int)])
        if strong.any():
            a, b = np.argmax(strong), len(strong) - np.argmax(strong[::-1])
            beats = beats[a:b]
        return beats

    def refine_times(self, times, win=0.03):
        """Pull every beat to the real attack with sample accuracy."""
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
            # the very beginning of the attack: the first point before the peak where the rise is > 25% of the peak
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
        # harmony changes on the downbeat
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
        """Timbre + harmony per beat — to compare spans with each other."""
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

    # ----------------------------------------------------------------- picking loops
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
                    continue      # bass, chords, voice or stabs are audible — not pure drums
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

    # ----------------------------------------------------------------- isolated parts
    def iso_metrics(self, t0: float, t1: float) -> dict:
        """Isolation metrics; short spans are judged together with their surroundings (at least 4 s)."""
        pad = max(0.0, (4.0 - (t1 - t0)) / 2)
        return iso.metrics(self.iso, max(0.0, t0 - pad), min(self.dur, t1 + pad))

    def sig_at(self, t0: float, t1: float) -> np.ndarray:
        """A "signature" of an off-grid span — in the same space as the signatures of beat-grid candidates."""
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
        """Spans where only `kind` is audible. On the beat grid, and for pads and vocals also where there
        is no grid (a beatless intro, ambient): those are cut exactly to free_bpm."""
        if kind == "drums":
            out = self.candidates(bars, "drums")
            for c in out:
                c.update(kind=kind, grade=2, rank=c["score"])
            return out
        out = []
        grid_ok = self.rhythm_problem() is None
        if grid_ok:
            for c in self.candidates(bars, "any", min_energy=0.08):     # pads and intros are quieter than the chorus
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
            floor = float(np.median(F["tot"])) - 25          # skip near-silence
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
    """The best n non-overlapping spans that don't sound alike.
    Order: "clean" (grade 2) first, then "with extras"; inside — by kind order, then by rank."""
    if not cands:
        return []
    rng = rng or np.random.default_rng()
    order = {k: i for i, k in enumerate(iso.KINDS)}
    for c in cands:   # a little randomness, as in real digging
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
    return chosen          # in priority order (best first)


def _fits(queue: list[dict], chosen: list[dict], tried: set, sims) -> "iter":
    """Candidates of one queue that may join `chosen`, best first: no time overlap with chosen spans,
    and not too similar to them — first at the strictest similarity threshold, then at looser ones."""
    for thresh in sims:
        for c in queue:
            if id(c) in tried:
                continue
            if any(not (c["t1"] <= d["t0"] or c["t0"] >= d["t1"]) for d in chosen):
                tried.add(id(c))                          # overlaps a chosen span — never fits again
                continue
            sig = c["sig"] / (np.linalg.norm(c["sig"]) + 1e-9)
            if any(float(np.dot(sig, d["sig"] / (np.linalg.norm(d["sig"]) + 1e-9))) > thresh for d in chosen):
                continue                                  # too similar for now — maybe at a looser threshold
            tried.add(id(c))
            yield c


def interleave_loops(queues: dict, n: int, accept, sims=(0.93, 0.975, 0.985)) -> list:
    """Loops of several "Only" types taken in turn, so the same type doesn't come several times in a row.
    queues — {kind: candidates sorted best first}; accept(c) -> the rendered loop, or None if it can't be
    used (then the next candidate of the same type takes its place).
    The first turn goes to the type whose best candidate is the cleanest (grade, then rank; ties — in
    KINDS order); then the types take turns in KINDS order. A type repeats in a row only when the other
    types have nothing left. -> accepted loops in the order they were picked."""
    order = {k: i for i, k in enumerate(iso.KINDS)}
    active = sorted((k for k in queues if queues[k]), key=lambda k: order.get(k, 9))
    if not active:
        return []
    first = max(active, key=lambda k: (queues[k][0].get("grade", 2), queues[k][0].get("_r", 0), -order.get(k, 9)))
    i = active.index(first)
    active = active[i:] + active[:i]
    chosen, out, tried, pos = [], [], set(), 0
    while len(out) < n and active:
        idx = pos % len(active)
        got = None
        for c in _fits(queues[active[idx]], chosen, tried, sims):
            chosen.append(c)
            got = accept(c)
            if got is not None:
                break
            chosen.pop()                                  # couldn't be rendered — try the next one
        if got is None:
            active.pop(idx)                               # this type has nothing left
            pos = idx
            continue
        out.append(got)
        pos = idx + 1
    return out


# --------------------------------------------------------------------------- tempo and rendering
def choose_target(real_bpm, bpm_min=60, bpm_max=120, fixed_bpm=None, max_stretch=0.05):
    """Choose the target tempo (a multiple of 5). The track may be taken at double/half tempo.
    -> (target_bpm, interpretation multiplier, ratio) or None"""
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
    """Pitching (like a turntable): faster = higher. -> (audio, exact actual ratio)"""
    fr = Fraction(1.0 / ratio).limit_denominator(1200)
    up, down = fr.numerator, fr.denominator
    z = signal.resample_poly(seg, up, down, axis=0).astype(np.float32)
    return z, down / up


# --------------------------------------------------------------------------- our own time-stretch
def _wsola(x: np.ndarray, out_len: int, keep: int, N: int = 1536, tol: int = 512) -> np.ndarray:
    """WSOLA with a time map: the first `keep` samples go 1:1 (the attack is untouched),
    the rest is evenly compressed/stretched to out_len."""
    L = len(x)
    if out_len <= 0:
        return np.zeros((0, x.shape[1]), np.float32)
    keep = min(keep, L, out_len)
    if L - keep < N // 2 or out_len - keep < N // 2:      # piece too short — just resample the tail
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
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / N)).astype(np.float32)   # periodic Hann: overlapping windows sum to 1
    pad = N
    xp = np.vstack([np.zeros((pad, x.shape[1]), np.float32), x, np.zeros((pad, x.shape[1]), np.float32)])
    xm = xp.mean(1)
    y = np.zeros((out_len + 2 * N, x.shape[1]), np.float32)
    ws = np.zeros(out_len + 2 * N, np.float32)
    slope = (L - keep) / (out_len - keep)

    def nominal(t):            # t — position in the output (without padding)
        return t if t < keep else keep + (t - keep) * slope

    prev = None
    k = 0
    while True:
        t_out = k * Hs - pad          # left edge of the frame in output coordinates
        if t_out >= out_len:
            break
        c = t_out + N // 2            # centre of the frame
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
                # only real peaks (not the edge of the window); ones close to the expected position are slightly preferred
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
    """Attacks (hits) inside [a, b) — in track samples."""
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
    """Stretch x[P0:P1] to out_len samples. Every attack lands exactly at (A-P0)/ratio,
    the attack itself is not stretched; only the "tails" between hits are."""
    F = int(0.008 * SR)                      # crossfade at the joints between pieces
    K = int(0.03 * SR)                       # how much of each attack stays untouched
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
    """The loop seam. pre — the sound right before the loop start: the end of the loop smoothly
    crossfades into it, so the loop becomes seamless (pads and vocals — a sustained sound doesn't click).
    Without pre — short micro-fades, as for drums (the attack on the first beat isn't smeared)."""
    loop = loop.copy()
    if pre is not None and len(pre) >= 64:
        xf = len(pre)
        t = np.linspace(0, np.pi / 2, xf, dtype=np.float32)[:, None]
        loop[-xf:] = loop[-xf:] * np.cos(t) + pre * np.sin(t)       # equal power
    else:
        fi, fo = int(0.0008 * SR), int(0.004 * SR)
        loop[:fi] *= np.linspace(0, 1, fi)[:, None]
        loop[-fo:] *= np.linspace(1, 0, fo)[:, None] ** 2
    if normalize:
        rms = float(np.sqrt(np.mean(loop ** 2)) + 1e-9)
        peak = float(np.max(np.abs(loop)) + 1e-9)
        loop *= min(10 ** (-16 / 20) / rms, 10 ** (-0.5 / 20) / peak)
    return loop


XFADE = {"pads": 0.25, "vocal": 0.03}      # length of the seamless crossfade, s (at most 1/8 of the loop)


SONG_KEY_KINDS = ("drums", "bass", "vocal")   # a single line or only hits — the mode can't be told, use the song's key


def _loop_key(an: TrackAnalysis, loop: np.ndarray, t0: float, t1: float, tuning_out: float,
              kind: str | None = None) -> dict:
    """Key of the loop: the loop itself and the surrounding +-20 s of the track vote.
    For isolated drums, bass and vocals — the key of the whole song."""
    tun, shift = wrap_tuning(tuning_out)
    if kind in SONG_KEY_KINDS:
        ts = key_scores(np.roll(an.track_chroma, shift) if shift else an.track_chroma)
        tonic, kmode, conf = key_from_scores(ts)
        te = an.chroma_raw.sum(1)
        ca, cb = int(t0 * an.cfps), max(int(t1 * an.cfps), int(t0 * an.cfps) + 1)
        return dict(key=key_name(tonic, kmode), camelot=camelot(tonic, kmode), key_conf=round(conf, 2),
                    tuning_cents=round(tun, 1), tonal=round(float(np.mean(te[ca:cb]) / (np.median(te) + 1e-9)), 2))
    lch, clarity = loop_chroma(loop, tun)
    # context: +-20 seconds around the loop (this way modulations inside a song are handled)
    fa, fb = max(0, int((t0 - 20) * an.cfps)), min(len(an.chroma), int((t1 + 20) * an.cfps) + 1)
    w = np.sqrt(np.linalg.norm(an.chroma_raw[fa:fb], axis=1))
    ctx = (an.chroma[fa:fb] * w[:, None]).sum(0)
    ctx = ctx / (ctx.sum() + 1e-9) if ctx.sum() > 0 else an.track_chroma
    tch = np.roll(ctx, shift) if shift else ctx
    # a loop inside a song is usually in the song's key, but if the span is clearly in another (bridge) — the loop wins
    ls, ts = key_scores(lch), key_scores(tch)
    tk = int(np.argmax(ts))
    # how many "notes" the span has relative to the whole track (a drum break has almost none)
    te = an.chroma_raw.sum(1)
    ca, cb = int(t0 * an.cfps), max(int(t1 * an.cfps), int(t0 * an.cfps) + 1)
    tonal_rel = float(np.mean(te[ca:cb]) / (np.median(te) + 1e-9))
    if tonal_rel < 0.35 or ls[tk] >= ls.max() - 0.25:
        # almost only drums, or the loop doesn't contradict the song -> the song's key
        tonic, kmode = tk % 12, ("maj" if tk < 12 else "min")
        conf = float(np.clip(0.5 * (ts[tk] + ls[tk]), 0, 1))
    else:                                  # a clear modulation — trust the loop
        wl = 0.45 + 0.35 * clarity
        tonic, kmode, conf = key_from_scores(wl * ls + (1 - wl) * ts)
    return dict(key=key_name(tonic, kmode), camelot=camelot(tonic, kmode), key_conf=round(conf, 2),
                tuning_cents=round(tun, 1), tonal=round(tonal_rel, 2))


_HP70 = signal.butter(2, 70, "highpass", fs=SR, output="sos").astype(np.float32)


def onset_start(an: TrackAnalysis, t: float) -> float:
    """The real start of the hit at beat t. From the peak of the hit, walk back to the point where the
    level first rose noticeably above the quiet before it (soft attack, flam) — at most 30 ms, never after t."""
    B = 24                                                    # 0.5 ms step
    a = int((t - 0.2) * SR)
    if a < 0:
        return t
    m = signal.sosfilt(_HP70, an.audio(a, (t + 0.025) * SR).sum(1))
    n = len(m) // B
    if n < 440:
        return t
    blk = np.abs(m[:n * B]).reshape(n, B).max(1)
    env = np.maximum(blk[:-1], blk[1:])                       # peak over 1 ms with a 0.5 ms step
    ti = int(round((t * SR - a) / B))
    floor = float(np.median(env[ti - 120:ti - 50])) + 1e-9    # the quiet 60-25 ms before the beat
    w0 = ti - 10
    pk = w0 + int(np.argmax(env[w0:ti + 30]))                 # the hit itself: -5...+15 ms
    peak = float(env[pk])
    if peak < 2.5 * floor:
        return t                                              # no clear hit — trust the grid
    thr = max(2.0 * floor, 0.1 * peak)
    j, lo = pk, ti - 60
    while j > lo and env[j - 1] >= thr:
        j -= 1
    return min(t, (a + j * B) / SR)


def attack_start(an: TrackAnalysis, t: float, look: float = 0.060, after: float = 0.015,
                 rise_db: float = 6.0, peak_db: float = 9.0) -> float:
    """The real start of a SOFT attack near beat t: a piano or pad chord in reverb, a sung note.
    The beat grid marks the loudest moment of an attack; a soft attack starts 20-50 ms earlier, which
    onset_start() (made for sharp drum hits) does not see. Looks for the first frame within `look`
    seconds before t (up to `after` after it) that rises `rise_db` above the level before it and grows
    to at least `peak_db`, then walks back to where the rise began. Never returns a time after t."""
    B = 96                                                    # 2 ms frames
    a = int((t - 0.25) * SR)
    if a < 0:
        return t
    x = signal.sosfilt(_HP70, an.audio(a, (t + 0.06) * SR).sum(1))
    n = len(x) // B
    if n < 150:
        return t
    e = 10 * np.log10((x[:n * B].reshape(n, B).astype(np.float64) ** 2).mean(1) + 1e-12)
    e = np.convolve(e, np.ones(3) / 3, "same")              # light smoothing (6 ms)
    ti = int(round(0.25 * SR / B))
    w0, w1 = ti - int(look * SR / B), ti + int(after * SR / B)
    base = float(np.percentile(e[ti - int(0.20 * SR / B):w0], 30))   # the level before the window
    j = next((j for j in range(w0, w1) if e[j] >= base + rise_db and e[j - 5] < base + rise_db - 3
              and e[j:j + 20].max() >= base + peak_db), None)
    if j is None:
        return t                                              # no soft attack here
    while j > w0 - 5 and e[j - 1] < e[j] and e[j - 1] > base + 1.5:
        j -= 1                                                # back to where the rise began
    return min(t, (a + j * B) / SR - 0.001)


def tail_leak(loop: np.ndarray) -> int:
    """How many last samples of the loop are already the start of the next hit (a "click" at the seam). 0 — clean."""
    m = np.abs(loop).max(1) if loop.ndim == 2 else np.abs(loop)
    B = SR // 1000
    n = len(m) // B
    if n < 150:
        return 0
    e = 20 * np.log10(m[:n * B].reshape(n, B).max(1) + 1e-9)
    body = float(np.median(e[-120:-20]))
    hot = np.nonzero(e[-15:-2] > body + 9)[0]                 # the last 15 ms (without the fade itself)
    return 0 if not len(hot) else len(m) - (n - 15 + int(hot[0])) * B


def _fade_tail(loop: np.ndarray, n: int) -> np.ndarray:
    n = min(len(loop) // 4, n)
    loop = loop.copy()
    loop[-n:] *= np.cos(np.linspace(0, np.pi / 2, n, dtype=np.float32))[:, None]
    return loop


def render_loop(an: TrackAnalysis, s: int, e: int, opts: dict) -> dict | None:
    """Cut the loop s..e (beat indices), fit the tempo, cut exactly on the grid.
    The boundaries sit on the real start of the hits; after cutting, the seam is checked: if the start
    of the next hit got into the end, the end is moved earlier and the loop is cut again (in "Pitching" —
    with a little more slowdown). Not fixed after 2 tries: drums are dropped, others get a soft fade-out.
    Pads and vocals (crossfaded seam) are handled by _render_blended().
    opts: mode, bpm_min/max, fixed_bpm, max_stretch, normalize, kind."""
    bt = an.beat_times
    t0, t1 = onset_start(an, float(bt[s])), onset_start(an, float(bt[e]))
    if opts.get("kind") in XFADE:
        return _render_blended(an, t0, t1, e - s, opts)
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
        t1 -= leak * (t1 - t0) / len(r["audio"]) + 0.002     # length of the leak in source seconds
    if r is None:
        return None
    if opts.get("kind") == "drums":
        return None                                           # clean breaks: better take another span
    r["audio"] = _fade_tail(r["audio"], tail_leak(r["audio"]) + SR // 500)
    r["seam"] = "fade"
    return r


def _render_blended(an: TrackAnalysis, t0: float, t1: float, beats: int, opts: dict) -> dict | None:
    """Pads and vocals. The end of such a loop crossfades into the sound right before its start
    (_finish), so the wrap is continuous by construction. What can still be heard is an attack that
    begins a few ms BEFORE the beat (soft chord, sung note): its beginning lies in the crossfade, is heard
    at the very end of the loop and is cut by the restart — a click. Moving only the end doesn't help, so
    both boundaries move to the real start of such attacks: the attack then belongs to the start of the
    loop and the end stays quiet (in "Pitching" this gives a slightly stronger slowdown).
    No tail fade here: on a crossfaded seam a fade only adds a dip right before the restart."""
    a0, a1 = attack_start(an, t0), attack_start(an, t1)
    r = _render_span(an, a0, a1, beats, opts)
    if r is not None:
        r["seam"] = "attack" if (t0 - a0 > 0.002 or t1 - a1 > 0.002) else "ok"
    return r


def _render_span(an: TrackAnalysis, t0: float, t1: float, beats: int, opts: dict) -> dict | None:
    """Render the span t0..t1 (seconds, `beats` beats long) at the target tempo: pitching (varispeed)
    or anchored time-stretch, then the seam (_finish), key and loop metadata."""
    real_bpm = 60.0 * beats / (t1 - t0)
    pick = choose_target(real_bpm, opts.get("bpm_min", 60), opts.get("bpm_max", 120),
                         opts.get("fixed_bpm"), opts.get("max_stretch", 0.05))
    if pick is None:
        return None
    target, mult, ratio = pick
    named_beats = beats * mult
    out_len = int(round(named_beats * 60.0 / target * SR))
    xf = min(int(XFADE.get(opts.get("kind"), 0) * SR), out_len // 8)

    pre = 0.003                                    # a bit before the attack, so its very beginning isn't eaten
    P0, P1 = (t0 - pre) * SR, (t1 - pre) * SR      # the loop in track samples
    tuning_out = an.tuning
    if opts.get("mode") == "vinyl":                # pitching: tempo changes together with pitch
        a = max(0, int(P0 - 0.3 * SR))
        z, real_ratio = varispeed(an.audio(a, P1 + 0.6 * SR), ratio)
        i0 = int(round((P0 - a) / real_ratio))
        loop = z[i0:i0 + out_len]
        head = z[max(0, i0 - xf):i0]
        tuning_out += 1200 * math.log2(real_ratio)
        engine = "pitch"
    else:                                          # stretching: hits stay in place, the pitch stays the same
        base = max(0, int(P0) - int(0.1 * SR))                 # only the needed piece of the track, not all of it
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
    """An off-grid span (pads, beatless vocals): exactly `bars` bars at target BPM, without stretching
    and without a pitch change — tempo doesn't matter for a sustained sound, length does. Seamless:
    the end crossfades into the sound before the start (no tail fade — it would add a dip)."""
    out_len = int(round(bars * 4 * 60.0 / target * SR))
    xf = min(int(XFADE.get(opts.get("kind"), 0.1) * SR), out_len // 8)
    P0 = max(xf, int(t0 * SR))
    loop = an.audio(P0, P0 + out_len)
    if len(loop) < out_len:
        loop = np.vstack([loop, np.zeros((out_len - len(loop), 2), np.float32)])
    loop = _finish(loop, an.audio(P0 - xf, P0), opts.get("normalize", True))
    t1 = (P0 + out_len) / SR
    return dict(audio=loop.astype(np.float32), **_loop_key(an, loop, P0 / SR, t1, an.tuning, opts.get("kind")),
                bpm=int(target), real_bpm=None, stretch_pct=0.0, pitch_st=0, bars=float(bars), beats=bars * 4,
                engine="cut", free=True, src_start=round(P0 / SR, 3), src_end=round(t1, 3), drums=False, seam="ok")


def waveform_peaks(x: np.ndarray, n=240):
    m = np.abs(x).max(1) if x.ndim == 2 else np.abs(x)
    k = max(1, len(m) // n)
    p = m[: k * n].reshape(-1, k).max(1)
    return [round(float(v), 3) for v in p / (p.max() + 1e-9)]
