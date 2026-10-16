"""
Finding isolated parts: "bass only", "vocals only", "pads only".

Works in two steps, both pure functions without shared state (easy to run in a separate process):
  features(...)  — per-frame features, once per track (~1-2 s per 5 minutes of audio);
  metrics(F, t0, t1) -> grade(m, kind) — a fast verdict for any span of the track:
      2 — "clean", 1 — "with extras" (something else is audible in the background), 0 — doesn't fit.

Main cues:
  * kick — a short hit in 35-160 Hz, louder than the sustained bass, which dies away quickly or
    "slides" down in pitch (a bass note holds its pitch);
  * bass line — sustained energy in the low end plus note attacks;
  * voice — the share of frames with one clear pitch above 160 Hz (YIN): chords and pads have no
    single pitch, a voice has one in 20-80 % of frames;
  * pads — no hits, no bass-note attacks, no leading monophonic line.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage, signal

from . import spec
from .spec import db as _db

KINDS = ("drums", "bass", "vocal", "pads")          # order = priority when several "Only" are selected
ISO_KINDS = ("bass", "vocal", "pads")                 # these need the features of this module
LETTER = {"drums": "D", "bass": "B", "vocal": "V", "pads": "P"}
KIND_LABEL = {"drums": "drums only", "bass": "bass only", "vocal": "vocals only", "pads": "pads only"}

FPS = 46.875            # common frame rate of the features (21.3 ms step)

# Thresholds tuned on labelled examples (16 tracks with "yes" and "no" regions).
# All levels are in dB relative to the median loudness of the span (so they don't depend on volume).
TH = dict(
    kick_veto=0.9,       # confidently detected kick hits per second — definitely a break
    snare_veto=1.0,      # snare hits per second (body 150-500 Hz + noise 1.5-8 kHz)
    # bass: the sustained low end is loud; "clean" — mids and highs much quieter
    bass_hlow=-10.0, bass_mid1=-14.0, bass_hi1=-35.0, bass_phi1=-22.0,
    bass_phi2=-18.0,     # "with extras": the percussive top (hats, snare) is no louder than this...
    bass_phi2s=-14.0, bass_sus2=4.0,   # ...or a bit louder if the mids are clearly sustained (FX, pad), not a break
    # snare/hat hits: the loudest percussive moment in 1.5-8 kHz relative to the mix and above its own floor.
    # Also catches dark, filtered breaks where the snare is thin and short
    bass_hit_veto=-10.0, bass_hit_jump=25.0, bass_hit1=-14.0,
    # vocals: the share of frames with one clear pitch above 160 Hz
    voc_clean=0.25, voc_hlow1=-35.0,   # "clean": a voice and an empty low end
    voc_mix=0.18, voc_hlow2=-20.0,     # "with extras": a voice with light bass or instruments
    voc_bass=0.45, voc_bass_phi=-18.0, # a voice over bass, but without drums
    voc_mid=-25.0, voc_phi=-9.0,       # a voice has mids at 0.8-3 kHz; a loud percussive top means a beat
    # pads: no bass and drums, no leading line, almost no new note onsets
    pads_hlow=-10.0, pads_voc=0.20, pads_snare=1.5, pads_phi=-14.0, pads_phi_quiet=-30.0,
    pads_notes1=0.3, pads_notes2=1.0, pads_voc1=0.12,   # something voice-like — only "with extras"
    # 1.3: a beat is never pads or vocals. At slow tempos a kick on beats 1 and 3 gives only ~0.7 hits/s,
    # below kick_veto, so pads/vocals have their own, stricter limit: at least this many kicks per second
    # AND at least beat_kick_n kicks in the span (2 random thumps in an ambient track are not a beat).
    # In real pads/vocals of the labelled tracks the rate stayed at or below 0.44/s (low piano notes).
    beat_kick=0.5, beat_kick_n=3,
    # 1.3: pads are harmony. A loud noisy top (crowd, applause, talk, wind) with almost no harmonic tone
    # is not a pad. Airy pads with breath noise stay: they are tonal (pitched ~0.9); dark pads have a quiet top.
    noise_phi=-14.0, noise_pitched=0.40,
    pads_melody=0.25,    # ranking only: a melody on top of the pad (new notes) lowers its place in the queue
)


def _yin(y8, W=400, hop=160, fs=8000, fmin=80, fmax=1000, thr=0.2):
    """YIN pitch tracker: fundamental frequency and aperiodicity every 20 ms (8 kHz signal).
    Returns (f0 Hz, aperiodicity 0..1 — low means a clear single pitch, frame RMS)."""
    n = (len(y8) - W) // hop
    if n <= 0:
        return np.zeros(0, np.float32), np.ones(0, np.float32), np.zeros(0, np.float32)
    tmax, tmin = fs // fmin, fs // fmax
    f0 = np.zeros(n, np.float32)
    ap = np.ones(n, np.float32)
    rms = np.zeros(n, np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(y8, W)[::hop][:n]
    taus = np.arange(1, tmax + 1)
    for a in range(0, n, 256):                          # small chunks — little temporary memory
        fr = frames[a:a + 256].astype(np.float64)
        F = np.fft.rfft(fr, 2 * W, axis=1)
        ac = np.fft.irfft(F * np.conj(F), axis=1)[:, :tmax + 1]
        E0 = ac[:, 0]
        d = 2 * (E0[:, None] - ac[:, 1:])
        cm = d * taus / (np.cumsum(d, 1) + 1e-12)          # cumulative mean normalised difference
        cm[:, :tmin - 1] = 1.0
        below = cm < thr
        has = below.any(1)
        first = np.where(has, below.argmax(1), cm.argmin(1))
        # walk down to the local minimum after the first threshold crossing (at most 1/4 of the period)
        idx = first[:, None] + np.arange(0, max(2, tmax // 4))[None]
        idx = np.minimum(idx, tmax - 1)
        seg = np.take_along_axis(cm, idx, 1)
        k = np.take_along_axis(idx, seg.argmin(1)[:, None], 1)[:, 0]
        k = np.clip(k, 1, tmax - 2)
        r = np.arange(len(fr))
        y0, y1, y2 = cm[r, k - 1], cm[r, k], cm[r, k + 1]
        den = y0 - 2 * y1 + y2                             # parabolic interpolation of the minimum
        off = np.where(np.abs(den) > 1e-9, 0.5 * (y0 - y2) / np.where(np.abs(den) > 1e-9, den, 1), 0)
        f0[a:a + len(fr)] = fs / (k + 1 + np.clip(off, -1, 1))
        ap[a:a + len(fr)] = np.where(has, y1, cm.min(1))
        rms[a:a + len(fr)] = np.sqrt(E0 / W)
    return f0, ap, rms


def features(y: np.ndarray, sr: int, mel_db: np.ndarray, mel_centers: np.ndarray, mel_fps: float,
             pitched: np.ndarray, pitched_fps: float) -> dict:
    """Per-frame features for the whole track. y — mono, sr — its sample rate (24 kHz);
    mel_db / pitched are already computed in TrackAnalysis (reused here)."""
    F = dict(fps=FPS, dur=len(y) / sr, pitched_fps=pitched_fps,
             pitched=None if pitched is None else pitched.astype(bool))   # None — the caller fills it in later

    # ---- mids and highs from the mel spectrum: total loudness, bands, percussive part (hats, snare)
    step = max(1, int(round(mel_fps / FPS)))
    E = 10 ** (mel_db / 10.0)
    n = len(E) // step
    E = E[:n * step].reshape(n, step, -1).mean(1)
    tot = _db(E.sum(1))
    band = lambda lo, hi: _db(E[:, (mel_centers >= lo) & (mel_centers < hi)].sum(1))
    F.update(tot=tot.astype(np.float32), mid=band(800, 3000).astype(np.float32),
             hi=band(3000, 8000).astype(np.float32))
    Hm, Pm = spec.hpss(np.sqrt(E).astype(np.float32), 9, 9)
    pb = lambda lo, hi: _db((Pm[:, (mel_centers >= lo) & (mel_centers < hi)] ** 2).sum(1))
    F["sn"] = pb(1500, 8000).astype(np.float32)        # percussive noise: hats, snare, shaker
    F["sb"] = pb(150, 500).astype(np.float32)          # snare "body"
    F["hmid"] = _db((Hm[:, (mel_centers >= 150) & (mel_centers < 4000)] ** 2).sum(1)).astype(np.float32)
    F["pmid"] = pb(150, 4000).astype(np.float32)
    # onsets of new notes in the sustained part at 250-4000 Hz: pads with soft attacks have almost none,
    # a melody (sax, one-note piano, voice) has several per second
    mb = (mel_centers >= 250) & (mel_centers < 4000)
    Hd = _db(Hm[:, mb] ** 2)
    up = np.zeros_like(Hd)
    up[2:] = np.clip(Hd[2:] - Hd[:-2], 0, 12)
    F["hon"] = up.mean(1).astype(np.float32)

    # ---- low end: 3 kHz signal, window 512 (5.9 Hz resolution), hop 64 (21.3 ms)
    yl = signal.resample_poly(y, 1, sr // 3000)          # float32 in -> float32 out
    S = spec.stft_mag(yl, 512, 64, 41)                  # up to 240 Hz
    del yl
    fl = np.arange(41) * 3000 / 512
    H, P = spec.hpss(S, 11, 9)
    b = (fl >= 35) & (fl < 160)
    m = min(len(S), n)
    Pl, Hl = _db((P[:m, b] ** 2).sum(1)), _db((H[:m, b] ** 2).sum(1))
    F["hlow"] = Hl.astype(np.float32)
    # low percussive peaks well above the local loudness are candidate kicks or bass-note attacks
    T = ndimage.median_filter(tot[:m], int(4 * FPS), mode="nearest")
    rel = Pl - T
    pk, _ = signal.find_peaks(rel, height=-14, prominence=10, distance=int(0.12 * FPS))
    kicks, notes = [], []
    sb = S[:, b]
    fb = fl[b]
    for p in pk:
        if Pl[p] <= Hl[p] or p + 21 >= len(S):
            continue
        # a kick drops in pitch ("drift") and/or dies away fast; a bass note holds its pitch and rings
        drift = fb[np.argmax(sb[p + 5])] / max(fb[np.argmax(sb[p + 1])], 1.0)
        e = _db((sb[p:p + 20] ** 2).sum(1))
        fall = np.nonzero(e < e.max() - 10)[0]
        dur = (fall[0] if len(fall) else 20) / FPS
        (kicks if (drift < 0.95 or dur < 0.16) else notes).append(p / FPS)
    F["kick_t"] = np.array(kicks, np.float32)                   # kick hits
    F["low_t"] = np.array(sorted(kicks + notes), np.float32)    # all attacks in the low end (kick + bass notes)

    # ---- voice: YIN on the 180-1500 Hz band (above the bass), 8 kHz, 20 ms step
    sos = signal.butter(4, [180, 1500], "bandpass", fs=sr, output="sos")
    # magnitude response like filtfilt (two passes), decimation to 8 kHz — streamed, no copies of the track
    y8 = spec.down_stream(lambda a, b: y[a:b], len(y), sr // 8000, sos, passes=2)
    f0, ap, rms = _yin(y8)
    F.update(f0=f0, ap=ap, vrms=rms, vfps=50.0)
    return F


def metrics(F: dict, t0: float, t1: float) -> dict:
    """Summary of the span t0..t1 (seconds). Levels are relative to the span's median loudness."""
    fps = F["fps"]
    a, b = int(t0 * fps), max(int(t1 * fps), int(t0 * fps) + 4)
    d = max(t1 - t0, 1e-3)
    T = float(np.median(F["tot"][a:b]))
    m = dict(
        mid=float(np.median(F["mid"][a:b]) - T),            # 0.8-3 kHz
        hi=float(np.median(F["hi"][a:b]) - T),              # 3-8 kHz
        hlow=float(np.median(F["hlow"][a:b]) - T) if a < len(F["hlow"]) else -60.0,   # sustained 35-160 Hz
        phi=float(np.percentile(F["sn"][a:b] - T, 90)),     # loud percussive top (hats/snare/noise)
        hit=float(np.percentile(F["sn"][a:b] - T, 99)),     # the loudest percussive moment
        hitjump=float(np.percentile(F["sn"][a:b], 99) - np.percentile(F["sn"][a:b], 50)),
        kick=float(((F["kick_t"] >= t0) & (F["kick_t"] < t1)).sum() / d),         # kicks per second
        kick_n=int(((F["kick_t"] >= t0) & (F["kick_t"] < t1)).sum()),           # kicks in the span
        lowev=float(((F["low_t"] >= t0) & (F["low_t"] < t1)).sum() / d),          # low attacks per second
        sustain=float(np.median(F["hmid"][a:b] - F["pmid"][a:b])),               # sustained vs percussive mids
    )
    h = F["hon"][a:b]
    pk, _ = signal.find_peaks(h, height=max(1.5, 2.5 * float(np.median(F["hon"]))), distance=int(0.08 * fps))
    m["notes"] = len(pk) / d                                  # new note onsets per second
    sn = np.minimum(F["sb"][a:b], F["sn"][a:b] + 6) - T
    pk, _ = signal.find_peaks(sn, height=-14, prominence=8, distance=int(0.1 * fps))
    m["snare"] = len(pk) / d                                  # snare-like hits per second
    pa, pb_ = int(t0 * F["pitched_fps"]), max(int(t1 * F["pitched_fps"]), int(t0 * F["pitched_fps"]) + 1)
    m["pitched"] = float(F["pitched"][pa:pb_].mean()) if pb_ > pa else 0.0   # share of frames with a harmonic tone
    va, vb = int(t0 * F["vfps"]), max(int(t1 * F["vfps"]), int(t0 * F["vfps"]) + 2)
    rm = F["vrms"][va:vb]
    if len(rm):
        v = (F["ap"][va:vb] < 0.25) & (rm > np.percentile(rm, 20)) & (F["f0"][va:vb] > 160)
        m["voiced"] = float(v.mean())                         # share of frames with one clear pitch > 160 Hz
    else:
        m["voiced"] = 0.0
    return m


def grade(m: dict, kind: str, th: dict = TH) -> int:
    """2 — "clean", 1 — "with extras", 0 — doesn't fit."""
    if m["kick"] >= th["kick_veto"]:
        return 0
    if kind in ("vocal", "pads") and m["kick"] >= th["beat_kick"] and m.get("kick_n", 99) >= th["beat_kick_n"]:
        return 0                          # repeating low punches with a fast attack = a beat
    if kind == "bass":
        if m["hlow"] < th["bass_hlow"] or m["snare"] >= th["snare_veto"]:
            return 0
        if m["hit"] > th["bass_hit_veto"] and m["hitjump"] > th["bass_hit_jump"]:
            return 0                      # a loud sharp hit in the highs is a kick/snare, not a bare bass
        if (m["mid"] <= th["bass_mid1"] and m["hi"] <= th["bass_hi1"] and m["phi"] <= th["bass_phi1"]
                and m["hit"] <= th["bass_hit1"]):
            return 2
        if m["phi"] <= th["bass_phi2"] or (m["phi"] <= th["bass_phi2s"] and m["sustain"] >= th["bass_sus2"]):
            return 1
        return 0
    if kind == "vocal":
        if m["mid"] < th["voc_mid"] or m["phi"] > th["voc_phi"]:
            return 0
        if m["voiced"] >= th["voc_clean"] and m["hlow"] <= th["voc_hlow1"]:
            return 2
        if m["voiced"] >= th["voc_mix"] and m["hlow"] <= th["voc_hlow2"]:
            return 1
        if m["voiced"] >= th["voc_bass"] and m["phi"] <= th["voc_bass_phi"] and m["snare"] < th["snare_veto"]:
            return 1
        return 0
    if kind == "pads":
        if (m["hlow"] > th["pads_hlow"] or m["voiced"] >= th["pads_voc"] or m["snare"] >= th["pads_snare"]
                or m["notes"] > th["pads_notes2"]):
            return 0
        if m["phi"] > th["pads_phi"] and m["hlow"] > th["pads_phi_quiet"]:
            return 0                      # a loud percussive top with a live low end is a beat, not a pad
        if m["phi"] > th["noise_phi"] and m["pitched"] < th["noise_pitched"]:
            return 0                      # noise without a tone (crowd, applause, talk, wind) is not a pad
        clean = m["notes"] <= th["pads_notes1"] and m["snare"] < th["snare_veto"] and m["voiced"] < th["pads_voc1"]
        return 2 if clean else 1
    return 0


def score(m: dict, kind: str) -> float:
    """How "isolated" the span sounds (0..1) — used to rank the spans that passed grade()."""
    if kind == "bass":
        return float(np.clip((-m["mid"] - 10) / 30, 0, 1) * 0.5 + np.clip((-m["hi"] - 25) / 30, 0, 1) * 0.5
                     - 0.3 * min(1.0, m["kick"]))
    if kind == "vocal":
        return float(np.clip(m["voiced"] / 0.6, 0, 1) * 0.7 + np.clip((-m["hlow"] - 5) / 30, 0, 1) * 0.3)
    if kind == "pads":
        return float(np.clip(m["pitched"], 0, 1) * 0.4 + np.clip(1 - m["voiced"] / 0.3, 0, 1) * 0.3
                     + np.clip((-m["phi"] - 20) / 25, 0, 1) * 0.3
                     - TH["pads_melody"] * np.clip(m["notes"] / 0.5, 0, 1))    # a melody on top — lower place
    return 0.0
