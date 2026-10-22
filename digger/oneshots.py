"""
One-shot extraction from drum loops.

A drum hit always starts with a sharp transient. So the exact start of every sample
is found as the very beginning of the transient's rise, and each sample ends just
before the next transient begins. The loop is analysed together with its own copy,
so a hit that straddles the end of the loop is cut correctly too.

Why several bands? In a dense break a new hit often starts while the previous hit's
tail is still loud — the full-band envelope barely moves, and that hit is masked.
But a hat has its energy high in the spectrum, a kick low: a transient that is
invisible in the full envelope is still a clear rise in its own band. So onsets are
pooled from three spectral bands plus the full-band envelope, each candidate must
show a real energy step in at least one band (and one big enough for that band),
and after the first pass every segment is rescanned for hits that were still masked
— they split the segment.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage, signal

HOP = 256              # hop between strength-curve frames (samples)
MIN_GAP = 0.020        # s — hits closer than this are one sound (flam, roll)
MERGE_GAP = 0.025      # s — onset candidates closer than this are the same hit
REFINE_WIN = 0.006     # s — search radius around a rough onset
ATTACK_BACK = 0.014    # s — how far before the envelope peak the start may be
SEP_MIN = 0.008        # s — minimum guaranteed distance between refined starts
END_MARGIN = 0.0015    # s — cut this much before the next transient
MIN_PART = 0.025       # s — a hit this close to a segment edge is part of it (tom click+body)
BANDS = (("low", 40.0, 250.0), ("mid", 250.0, 2000.0), ("high", 2000.0, 15000.0))


def _envelope(x, sr, win=0.0007):
    """A short max-filter keeps the body of a low kick from "wobbling"; a light
    smoothing then gives a clean envelope to work with."""
    w = max(1, int(round(win * sr)))
    env = ndimage.maximum_filter1d(np.abs(x), w)
    return ndimage.uniform_filter1d(env, w)


def _analyse(x, sr):
    """One STFT pass over the mono signal -> (onset curves, band power tops, scale).
    Each curve is the positive dB rise of a band's power / the envelope, with the
    slow background removed — a transient is a fast, large jump; a decay (however
    long) never rises and stays at zero. Tops are the 99.5 percentile of each
    band's power (the absolute scale of "a real hit" in that band); scale is the
    p99 of the envelope."""
    _, _, Z = signal.stft(x, fs=sr, nperseg=1024, noverlap=1024 - HOP,
                          boundary="zeros", padded=True)
    mag = np.abs(Z)
    freqs = np.fft.rfftfreq(1024, 1.0 / sr)
    w = max(3, int(round(0.12 * sr / HOP)))
    curves, gtops = {}, {}
    for name, lo, hi in BANDS:
        idx = (freqs >= lo) & (freqs < hi)
        p = (mag[idx] ** 2).sum(0)
        gtops[name] = float(np.percentile(p, 99.5)) if len(p) else 1.0
        mdb = 10.0 * np.log10(p + 1e-12)
        f = np.maximum(0.0, mdb[1:] - mdb[:-1])
        f = np.concatenate([[0.0], f])
        f = f - ndimage.uniform_filter1d(f, w)
        curves[name] = ndimage.gaussian_filter1d(np.maximum(f, 0.0), 1.0)
    env = _envelope(x, sr)
    scale = float(np.percentile(env, 99))
    edb = 20.0 * np.log10(np.maximum(env, 1e-6))
    r = np.zeros_like(edb)
    r[1:] = edb[1:] - edb[:-1]
    r = np.maximum(r, 0.0)
    n = len(r) // HOP
    f = r[:n * HOP].reshape(n, HOP).max(1)
    if len(r) > n * HOP:
        f = np.concatenate([f, [float(r[n * HOP:].max())]])
    f = f - ndimage.uniform_filter1d(f, w)
    curves["env"] = ndimage.gaussian_filter1d(np.maximum(f, 0.0), 1.0)
    return curves, gtops, scale


def _peaks(curves, fps, min_gap):
    """Onset candidates: local maxima of every curve above its own local background.
    -> sorted (peak_frame, [bands where it fired])"""
    a = []
    for name, s in curves.items():
        top = float(np.percentile(s, 99.5))
        if top <= 0:
            continue
        bg = ndimage.uniform_filter1d(s, max(3, int(round(0.16 * fps))))
        pk, _ = signal.find_peaks(s, distance=max(2, int(round(min_gap * fps))))
        pk = pk[s[pk] > np.maximum(3.5 * bg[pk], 0.015 * top) + 1e-9]
        a += [(int(q), name) for q in pk]
    a.sort()
    out = []
    for f, name in a:
        if out and f - out[-1][0] < max(2, int(round(MERGE_GAP * fps))):
            out[-1][1].add(name)
            continue
        out.append([f, {name}])
    return out


def _energy_step(sr, st, slices, scale, gtops):
    """Is there a real energy step at `st`? The level right after the hit must be
    clearly above the level right before it in at least one band (or the full
    envelope), and big enough relative to the whole signal — a new sound's own band
    always rises, even when the previous hit's tail still masks the full envelope,
    while a resonant ripple of one instrument stays below the bar. Accept:
    * the envelope rises clearly (a kick with no spectral spread), or
    * a band rises sharply AND its level is significant vs that band's global top.
    slices — (name, array, units) windows around st; gtops — per-band power tops
    (99.5 percentile) of the whole searched signal; scale — its p99 envelope."""
    for name, arr, units in slices:
        if arr is None or len(arr) < 8:
            continue
        if units == "samples":
            mid = len(arr) // 2
            lo, hi = mid - int(0.006 * sr), mid + int(0.020 * sr)
            bef = float(arr[max(0, lo):mid].max()) if mid > 0 else 0.0
            aft = float(arr[mid:min(len(arr), hi)].max())
            if aft > max(2.0 * bef, bef + 0.0015, 0.05 * scale):
                return True
        else:
            mid = len(arr) // 2
            bef = float(arr[max(0, mid - 2):mid].max()) if mid > 0 else 0.0
            aft = float(arr[mid:min(len(arr), mid + 4)].max())
            gtop = gtops.get(name, 1.0)
            if aft > max(4.0 * bef, bef + 0.0015, 0.03 * gtop):
                return True
    return False


def _band_env_slices(x, sr, st, scale, win=0.022):
    """(name, array, units) around st: full envelope in samples and per-band power
    curves at STFT frame rate (sr / 128), for _energy_step."""
    a = max(0, st - int(win * sr))
    b = min(len(x), st + int(win * sr) + 1)
    seg = x[a:b]
    if len(seg) < 8:
        return [("env", None, "samples")]
    env = _envelope(seg, sr, 0.0008)
    slices = [("env", env, "samples")]
    _, _, Z = signal.stft(seg, fs=sr, nperseg=512, noverlap=512 - 128,
                          boundary="zeros", padded=True)
    mag = np.abs(Z) ** 2
    freqs = np.fft.rfftfreq(512, 1.0 / sr)
    for name, lo, hi in BANDS:
        idx = (freqs >= lo) & (freqs < hi)
        p = mag[idx].sum(0)
        if p.max() <= 0:
            slices.append((name, None, "frames"))
            continue
        slices.append((name, p, "frames"))
    return slices


def _refine(x, sr, rough):
    """Exact start of the transient near `rough` (seconds) — a sample index.
    The start is the last point before the envelope peak that still sits at the
    background level: that is where the hit physically begins."""
    c = int(round(rough * sr))
    w = int(round(REFINE_WIN * sr))
    a, b = max(0, c - w), min(len(x), c + w)
    if b - a < 4:
        return a
    env = _envelope(x[a:b], sr, 0.0005)
    k = int(np.argmax(env))
    pk = float(env[k])
    if pk <= 0:
        return a + k
    base = float(np.percentile(env, 15))
    thr = base + 0.12 * (pk - base)
    lo = max(0, k - int(round(ATTACK_BACK * sr)))
    below = np.where(env[lo:k + 1] <= thr)[0]
    k0 = lo + (int(below[-1]) + 1 if len(below) else 0)
    return a + k0


def _detect_hits(sig, sr, min_gap, analysis=None):
    """Refined hit starts inside `sig` (mono) that pass the energy-step check.
    analysis — precomputed (curves, gtops, scale) from _analyse, or None to compute
    it for `sig`. Returns sorted sample indices."""
    if analysis is None:
        analysis = _analyse(sig, sr)
    curves, gtops, scale = analysis
    fps = sr / HOP
    sep = int(round(SEP_MIN * sr))
    out = []
    for f, _ in _peaks(curves, fps, min_gap):
        st = min(max(_refine(sig, sr, f / fps), 0), len(sig) - 1)
        if not _energy_step(sr, st, _band_env_slices(sig, sr, st, scale), scale, gtops):
            continue
        if out and st - out[-1] < sep:
            continue
        out.append(st)
    return out


def detect_oneshots(x, sr=48000, min_gap=MIN_GAP):
    """Split a drum loop into one-shots. x — float (N, channels), sr — sample rate.
    -> list of (start, end) sample indices. `end` may be > len(x) for the last hit
    (it wraps across the loop end — cut_oneshot knows how to take it)."""
    n = len(x)
    if n < sr // 10:
        return []
    mono = x.mean(1, dtype=np.float32) if x.ndim == 2 else x
    # two copies in a row: a hit at the very end of the loop is seen whole; hits
    # are normalised into the first copy
    xx = np.concatenate([mono, mono])
    analysis = _analyse(xx, sr)
    starts = sorted({s % n for s in _detect_hits(xx, sr, min_gap, analysis) if s < n})
    if len(starts) < 2:
        return []
    # every segment must contain one hit: rescan the interior of each segment
    # (unwrapped) with the same detector; a masked hit inside splits it (>= MIN_PART
    # from the edges, so a tom's own click+body stays one sound)
    min_part = int(MIN_PART * sr)
    changed = True
    while changed:
        changed = False
        starts_set = set(starts)
        insert = []
        for a, b in zip(starts, starts[1:] + [starts[0] + n]):
            if b - a < 2 * min_part:
                continue
            if b > n:
                piece = np.concatenate([mono[a:], mono[:b - n]])
            else:
                piece = mono[a:b]
            for st in _detect_hits(piece, sr, min_gap):
                pos = a + st
                if pos >= n:
                    pos -= n
                if min_part <= st <= len(piece) - min_part and pos not in starts_set:
                    insert.append(pos)
                    starts_set.add(pos)
                    changed = True
        if insert:
            starts = sorted(starts_set)
    end_margin = int(round(END_MARGIN * sr))
    out = [(starts[i], starts[i + 1] - end_margin) for i in range(len(starts) - 1)]
    out.append((starts[-1], starts[0] + n - end_margin))
    return out


def cut_oneshot(x, s, e, sr, fade_in=0.0012, fade_out=0.004):
    """The audio of one hit [s, e) (e may wrap past the loop end).
    Micro-fades at the edges: the attack is untouched, the tail gets a few ms of
    release so a short hit doesn't click when triggered."""
    n = len(x)
    if e <= n:
        y = x[s:e].copy()
    else:
        y = np.concatenate([x[s:], x[:e - n]])
    if not len(y):
        return y
    fi = min(int(round(fade_in * sr)), len(y) // 4)
    fo = min(int(round(fade_out * sr)), len(y) // 4)
    if fi > 0:
        y[:fi] *= np.linspace(0, 1, fi)[:, None] if y.ndim == 2 else np.linspace(0, 1, fi)
    if fo > 0:
        y[-fo:] *= (np.linspace(1, 0, fo) ** 2)[:, None] if y.ndim == 2 else np.linspace(1, 0, fo) ** 2
    return y
