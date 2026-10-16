"""
Общие кирпичики спектрального анализа — без состояния, экономно по памяти:
длинные треки обрабатываются кусками, и весь трек нигде не копируется во float64.
"""
from __future__ import annotations

import numpy as np
import scipy.fft as sfft
from scipy import signal

# Потоков на одно FFT. В процессах конвейера ставим меньше, чтобы процессы не дрались за ядра.
FFT_WORKERS = -1
BLOCK = 480000          # кусок для потоковой обработки (10 с при 48 кГц)


def db(e):
    return 10 * np.log10(e + 1e-10)


def stft_reduce(y: np.ndarray, n_fft: int, hop: int, fn, max_bin: int | None = None):
    """STFT по кускам: fn(|X| куска) -> массив строк; результаты склеиваются.
    Полный спектр в памяти не держится (например, сразу считаем mel-полосы),
    и трек не копируется ради отступов по краям (кадры центрированы, края — нули)."""
    n, pad = len(y), n_fft // 2
    n_frames = 1 + n // hop
    win = np.hanning(n_fft).astype(np.float32)
    nb = n_fft // 2 + 1 if max_bin is None else max_bin
    chunk = max(64, (1 << 20) // n_fft)                  # ~4 МБ на кусок кадров
    out = None                                           # результат пишем сразу на место, без склейки
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
    """Модуль спектра (кадры, бины), float32."""
    return stft_reduce(y, n_fft, hop, lambda X: X, max_bin)


def hpss(S: np.ndarray, kt: int, kf: int, chunk: int = 1024):
    """Медианное разделение: тянущееся (медиана по времени kt кадров) и ударное (по частоте kf бинов).
    По кускам с перекрытием — результат тот же, что и на всём спектре сразу. -> (H, P)"""
    H, P = np.empty_like(S), np.empty_like(S)
    for a, b, h, p in _hpss_chunks(S, kt, kf, chunk):
        H[a:b], P[a:b] = h, p
    return H, P


def hpss_inplace(S: np.ndarray, kt: int, kf: int, chunk: int = 1024):
    """То же, но S заменяется на тянущуюся часть прямо на месте (без второй копии спектра).
    -> (энергия тянущейся части по кадрам, энергия ударной части по кадрам)"""
    n = len(S)
    eh, ep = np.empty(n, np.float32), np.empty(n, np.float32)
    for a, b, h, p in _hpss_chunks(S, kt, kf, chunk):
        eh[a:b], ep[a:b] = (h * h).sum(1), (p * p).sum(1)
        S[a:b] = h
    return eh, ep


def median(X: np.ndarray, k: int, axis: int) -> np.ndarray:
    """Скользящая медиана окна k вдоль оси 0 или 1 (края — повтор крайнего значения, как
    mode="nearest" у ndimage.median_filter, результат тот же бит в бит). Через np.partition —
    в 2–3 раза быстрее ndimage; блоками строк, чтобы временная память была ~8 МБ."""
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
    """Куски разделения. Перед тем как кусок [a, b) перезапишут, его хвост (нужный следующему
    куску как контекст медианы) сохраняется — поэтому можно писать результат прямо в S."""
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
    """Сигнал get(a, b) -> float32 [a, b) длиной n: (по желанию) IIR-фильтр sos в `passes` проходов
    (passes=2 — АЧХ как у filtfilt) и понижение частоты в `down` раз — всё потоково, кусками.
    Результат совпадает с обработкой всего сигнала сразу, но целиком в памяти лежит только выход."""
    ctx -= ctx % down
    block -= block % down
    sos = None if sos is None else np.asarray(sos, np.float32)
    zi = [np.zeros((sos.shape[0], 2), np.float32) for _ in range(passes)] if sos is not None else []
    out = np.empty(-(-n // down), np.float32)
    hist, h0, done = np.zeros(0, np.float32), 0, 0          # done — сколько входных сэмплов уже «закрыто»
    for a in range(0, n, block):
        b = min(n, a + block)
        v = get(a, b)
        for k in range(len(zi)):
            v, zi[k] = signal.sosfilt(sos, v, zi=zi[k])
        hist = np.concatenate([hist, v.astype(np.float32, copy=False)])
        r = signal.resample_poly(hist, 1, down) if down > 1 else hist
        end = b if b == n else b - ctx                      # хвост ещё ждёт правый контекст
        end = n if b == n else end - (end - h0) % down
        j0, j1 = (done - h0) // down, -(-(end - h0) // down)
        out[done // down:done // down + (j1 - j0)] = r[j0:j1]
        done = h0 + j1 * down
        keep = max(0, done - ctx)
        keep -= (keep - h0) % down
        hist, h0 = hist[keep - h0:], keep
    return out
