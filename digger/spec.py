"""
Shared building blocks of spectral analysis — stateless and memory-friendly:
long tracks are processed in chunks, and the whole track is never copied to float64.
"""
from __future__ import annotations

import numpy as np
import scipy.fft as sfft
from scipy import signal

# Threads per FFT. Analysis processes of the pipeline set fewer, so they don't fight over cores.
FFT_WORKERS = -1
BLOCK = 480000          # chunk size for streaming processing (10 s at 48 kHz)


def db(e):
    """Power -> decibels (with a tiny floor so silence doesn't become -inf)."""
    return 10 * np.log10(e + 1e-10)


def stft_reduce(y: np.ndarray, n_fft: int, hop: int, fn, max_bin: int | None = None):
    """Chunked STFT: fn(|X| of a chunk) -> array of rows; the results are concatenated.
    The full spectrum is never kept in memory (e.g. mel bands are computed right away),
    and the track is not copied to add padding at the edges (frames are centred, edges are zeros)."""
    n, pad = len(y), n_fft // 2
    n_frames = 1 + n // hop
    win = np.hanning(n_fft).astype(np.float32)
    nb = n_fft // 2 + 1 if max_bin is None else max_bin
    chunk = max(64, (1 << 20) // n_fft)                  # ~4 MB worth of frames per chunk
    out = None                                           # results are written in place, no concatenation
    for i0 in range(0, n_frames, chunk):
        i1 = min(n_frames, i0 + chunk)
        lo, hi = i0 * hop - pad, (i1 - 1) * hop - pad + n_fft
        seg = np.zeros(hi - lo, np.float32)
        a, b = max(lo, 0), min(hi, n)
        if b > a:
            seg[a - lo:b - lo] = y[a:b]
        fr = np.lib.stride_tricks.sliding_window_view(seg, n_fft)[::hop]
        X = np.abs(sfft.rfft(fr * win, axis=1, workers=FFT_WORKERS)[:, :nb]).astype(np.float32)
        r = fn(X)
        if out is None:
            out = np.empty((n_frames,) + r.shape[1:], r.dtype)
        out[i0:i1] = r
    return out if out is not None else np.zeros((0, nb), np.float32)


def stft_mag(y: np.ndarray, n_fft: int, hop: int, max_bin: int | None = None) -> np.ndarray:
    """Magnitude spectrum (frames, bins), float32."""
    return stft_reduce(y, n_fft, hop, lambda X: X, max_bin)


def hpss(S: np.ndarray, kt: int, kf: int, chunk: int = 1024):
    """Median harmonic/percussive separation: sustained part (median over kt frames in time) and
    percussive part (median over kf bins in frequency). Done in overlapping chunks — the result is
    the same as on the whole spectrum at once. -> (H, P)"""
    H, P = np.empty_like(S), np.empty_like(S)
    for a, b, h, p in _hpss_chunks(S, kt, kf, chunk):
        H[a:b], P[a:b] = h, p
    return H, P


def hpss_inplace(S: np.ndarray, kt: int, kf: int, chunk: int = 1024):
    """Same, but S is replaced by its sustained part in place (no second copy of the spectrum).
    -> (energy of the sustained part per frame, energy of the percussive part per frame)"""
    n = len(S)
    eh, ep = np.empty(n, np.float32), np.empty(n, np.float32)
    for a, b, h, p in _hpss_chunks(S, kt, kf, chunk):
        eh[a:b], ep[a:b] = (h * h).sum(1), (p * p).sum(1)
        S[a:b] = h
    return eh, ep


def median(X: np.ndarray, k: int, axis: int) -> np.ndarray:
    """Sliding median with window k along axis 0 or 1 (edges repeat the edge value, like
    mode="nearest" of ndimage.median_filter; the result is bit-identical). Uses np.partition —
    2-3x faster than ndimage; works on blocks of rows so temporary memory stays ~8 MB."""
    p = k // 2
    n, m = X.shape
    out = np.empty_like(X)
    if axis == 0:
        Xp = np.concatenate([np.repeat(X[:1], p, 0), X, np.repeat(X[-1:], p, 0)])
        rb = max(8, 2_000_000 // max(1, m * k))
        for i in range(0, n, rb):
            W = np.lib.stride_tricks.sliding_window_view(Xp[i:i + rb + 2 * p], k, axis=0)
            out[i:i + rb] = np.partition(W, p, axis=-1)[..., p]
    else:
        rb = max(8, 2_000_000 // max(1, m * k))
        for i in range(0, n, rb):
            R = X[i:i + rb]
            Rp = np.concatenate([np.repeat(R[:, :1], p, 1), R, np.repeat(R[:, -1:], p, 1)], 1)
            W = np.lib.stride_tricks.sliding_window_view(Rp, k, axis=1)
            out[i:i + rb] = np.partition(W, p, axis=-1)[..., p]
    return out


def _hpss_chunks(S, kt, kf, chunk):
    """Chunks of the separation. Before chunk [a, b) is overwritten, its tail (needed by the next
    chunk as median context) is saved — that's why the result can be written straight into S.
    Yields (a, b, harmonic part, percussive part) using soft (Wiener-like) masks."""
    n = len(S)
    pad = kt // 2 + 1
    tail = S[0:0].copy()
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        X = np.concatenate([tail, S[a:min(n, b + pad)]])
        o = len(tail)
        H = median(X, kt, 0)[o:o + b - a]
        P = median(X[o:o + b - a], kf, 1)
        m = P * P / (H * H + P * P + 1e-12)
        tail = S[max(0, b - pad):b].copy()
        Xs = X[o:o + b - a]
        yield a, b, Xs * (1 - m), Xs * m


def down_stream(get, n: int, down: int, sos: np.ndarray | None = None, passes: int = 1,
                block: int = BLOCK, ctx: int = 2048) -> np.ndarray:
    """Signal get(a, b) -> float32 samples [a, b) of total length n: optional IIR filter sos applied
    `passes` times (passes=2 gives the magnitude response of filtfilt) and decimation by `down` —
    all streamed in chunks. The result matches processing the whole signal at once, but only the
    output is ever held in memory in full."""
    ctx -= ctx % down
    block -= block % down
    sos = None if sos is None else np.asarray(sos, np.float32)
    zi = [np.zeros((sos.shape[0], 2), np.float32) for _ in range(passes)] if sos is not None else []
    out = np.empty(-(-n // down), np.float32)
    hist, h0, done = np.zeros(0, np.float32), 0, 0          # done — how many input samples are final
    for a in range(0, n, block):
        b = min(n, a + block)
        v = get(a, b)
        for k in range(len(zi)):
            v, zi[k] = signal.sosfilt(sos, v, zi=zi[k])
        hist = np.concatenate([hist, v.astype(np.float32, copy=False)])
        r = signal.resample_poly(hist, 1, down) if down > 1 else hist
        end = b if b == n else b - ctx                      # the tail still waits for right-hand context
        end = n if b == n else end - (end - h0) % down
        j0, j1 = (done - h0) // down, -(-(end - h0) // down)
        out[done // down:done // down + (j1 - j0)] = r[j0:j1]
        done = h0 + j1 * down
        keep = max(0, done - ctx)
        keep -= (keep - h0) % down
        hist, h0 = hist[keep - h0:], keep
    return out
