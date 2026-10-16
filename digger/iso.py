"""
Поиск изолированных партий: «только бас», «только вокал», «только пэды».

Устроено в два шага, оба — чистые функции без общего состояния
(удобно запускать в отдельном процессе):
  features(...)  — признаки по кадрам, один раз на трек (≈1–2 с на 5 минут);
  metrics(F, t0, t1) -> grade(m, kind) — быстрая оценка любого куска:
      2 — «чисто», 1 — «с примесью», 0 — не подходит.

Главные признаки:
  * бочка — короткий удар в 35–160 Гц, громче тянущегося баса, который быстро
    гаснет или «съезжает» вниз по высоте (у басовой ноты тон держится);
  * басовая партия — тянущаяся энергия в низу + атаки нот;
  * голос — доля кадров с одним чётким тоном выше 160 Гц (YIN): у аккордов
    и пэдов одиночного тона нет, у голоса — 20–80 % кадров;
  * пэды — нет ударов, нет атак басовых нот, нет ведущей одноголосой линии.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage, signal

from . import spec
from .spec import db as _db

KINDS = ("drums", "bass", "vocal", "pads")          # порядок = приоритет при нескольких «Только»
ISO_KINDS = ("bass", "vocal", "pads")                 # им нужны признаки этого модуля
LETTER = {"drums": "D", "bass": "B", "vocal": "V", "pads": "P"}
NAME_RU = {"drums": "только барабаны", "bass": "только бас", "vocal": "только вокал", "pads": "только пэды"}

FPS = 46.875            # общая частота кадров признаков (шаг 21.3 мс)

# Пороги подобраны на размеченных примерах (16 треков с участками «да» и «нет»)
TH = dict(
    kick_veto=0.9,       # уверенно найденных ударов бочки в секунду — точно брейк
    snare_veto=1.0,      # ударов малого (тело 150–500 Гц + шум 1.5–8 кГц) в секунду
    # бас: тянущийся низ громкий; «чисто» — середина и верх намного тише
    bass_hlow=-10.0, bass_mid1=-14.0, bass_hi1=-35.0, bass_phi1=-22.0,
    bass_phi2=-18.0,     # «с примесью»: ударный верх (хэты, малый) не громче этого…
    bass_phi2s=-14.0, bass_sus2=4.0,   # …или чуть громче, если середина явно тянущаяся (FX, пэд), а не брейк
    # удары малого/хэта: самый громкий ударный миг в 1.5–8 кГц относительно микса и над своим фоном.
    # Ловит и тёмные, отфильтрованные брейки, где малый тонкий и короткий
    bass_hit_veto=-10.0, bass_hit_jump=25.0, bass_hit1=-14.0,
    # вокал: доля кадров с одним чётким тоном выше 160 Гц
    voc_clean=0.25, voc_hlow1=-35.0,   # «чисто»: голос, а внизу пусто
    voc_mix=0.18, voc_hlow2=-20.0,     # «с примесью»: голос с лёгким басом или инструментами
    voc_bass=0.45, voc_bass_phi=-18.0, # голос поверх баса, но без ударных
    voc_mid=-25.0, voc_phi=-9.0,       # у голоса есть середина 0.8–3 кГц; громкий ударный верх — это бит
    # пэды: нет баса и ударных, нет ведущей линии, почти нет начал новых нот
    pads_hlow=-10.0, pads_voc=0.20, pads_snare=1.5, pads_phi=-14.0, pads_phi_quiet=-30.0,
    pads_notes1=0.3, pads_notes2=1.0, pads_voc1=0.12,   # что-то голосоподобное — только «с примесью»
)


def _yin(y8, W=400, hop=160, fs=8000, fmin=80, fmax=1000, thr=0.2):
    """YIN: основной тон и апериодичность каждые 20 мс (сигнал 8 кГц)."""
    n = (len(y8) - W) // hop
    if n <= 0:
        return np.zeros(0, np.float32), np.ones(0, np.float32), np.zeros(0, np.float32)
    tmax, tmin = fs // fmin, fs // fmax
    f0 = np.zeros(n, np.float32)
    ap = np.ones(n, np.float32)
    rms = np.zeros(n, np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(y8, W)[::hop][:n]
    taus = np.arange(1, tmax + 1)
    for a in range(0, n, 256):                          # маленькие куски — мало временной памяти
        fr = frames[a:a + 256].astype(np.float64)
        F = np.fft.rfft(fr, 2 * W, axis=1)
        ac = np.fft.irfft(F * np.conj(F), axis=1)[:, :tmax + 1]
        E0 = ac[:, 0]
        d = 2 * (E0[:, None] - ac[:, 1:])
        cm = d * taus / (np.cumsum(d, 1) + 1e-12)          # нормированная разностная функция
        cm[:, :tmin - 1] = 1.0
        below = cm < thr
        has = below.any(1)
        first = np.where(has, below.argmax(1), cm.argmin(1))
        # спуск до локального минимума после первого пересечения порога (не дальше 1/4 периода)
        idx = first[:, None] + np.arange(0, max(2, tmax // 4))[None]
        idx = np.minimum(idx, tmax - 1)
        seg = np.take_along_axis(cm, idx, 1)
        k = np.take_along_axis(idx, seg.argmin(1)[:, None], 1)[:, 0]
        k = np.clip(k, 1, tmax - 2)
        r = np.arange(len(fr))
        y0, y1, y2 = cm[r, k - 1], cm[r, k], cm[r, k + 1]
        den = y0 - 2 * y1 + y2
        off = np.where(np.abs(den) > 1e-9, 0.5 * (y0 - y2) / np.where(np.abs(den) > 1e-9, den, 1), 0)
        f0[a:a + len(fr)] = fs / (k + 1 + np.clip(off, -1, 1))
        ap[a:a + len(fr)] = np.where(has, y1, cm.min(1))
        rms[a:a + len(fr)] = np.sqrt(E0 / W)
    return f0, ap, rms


def features(y: np.ndarray, sr: int, mel_db: np.ndarray, mel_centers: np.ndarray, mel_fps: float,
             pitched: np.ndarray, pitched_fps: float) -> dict:
    """Признаки по кадрам для всего трека. y — моно, sr — его частота (24 кГц);
    mel_db / pitched уже посчитаны в TrackAnalysis (переиспользуем)."""
    F = dict(fps=FPS, dur=len(y) / sr, pitched_fps=pitched_fps,
             pitched=None if pitched is None else pitched.astype(bool))   # None — допишет вызывающий

    # ---- середина и верх из mel: общая громкость, полосы, ударная часть (хэты, малый)
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
    F["sn"] = pb(1500, 8000).astype(np.float32)        # ударный шум: хэты, малый, шейкер
    F["sb"] = pb(150, 500).astype(np.float32)          # «тело» малого
    F["hmid"] = _db((Hm[:, (mel_centers >= 150) & (mel_centers < 4000)] ** 2).sum(1)).astype(np.float32)
    F["pmid"] = pb(150, 4000).astype(np.float32)
    # начала новых нот в тянущейся части 250–4000 Гц: у пэдов с плавной атакой их почти нет,
    # у мелодии (сакс, фортепиано по одной ноте, голос) — несколько в секунду
    mb = (mel_centers >= 250) & (mel_centers < 4000)
    Hd = _db(Hm[:, mb] ** 2)
    up = np.zeros_like(Hd)
    up[2:] = np.clip(Hd[2:] - Hd[:-2], 0, 12)
    F["hon"] = up.mean(1).astype(np.float32)

    # ---- низ: сигнал 3 кГц, окно 512 (5.9 Гц), шаг 64 (21.3 мс)
    yl = signal.resample_poly(y, 1, sr // 3000)          # float32 на входе -> float32
    S = spec.stft_mag(yl, 512, 64, 41)                  # до 240 Гц
    del yl
    fl = np.arange(41) * 3000 / 512
    H, P = spec.hpss(S, 11, 9)
    b = (fl >= 35) & (fl < 160)
    m = min(len(S), n)
    Pl, Hl = _db((P[:m, b] ** 2).sum(1)), _db((H[:m, b] ** 2).sum(1))
    F["hlow"] = Hl.astype(np.float32)
    T = ndimage.median_filter(tot[:m], int(4 * FPS), mode="nearest")
    rel = Pl - T
    pk, _ = signal.find_peaks(rel, height=-14, prominence=10, distance=int(0.12 * FPS))
    kicks, notes = [], []
    sb = S[:, b]
    fb = fl[b]
    for p in pk:
        if Pl[p] <= Hl[p] or p + 21 >= len(S):
            continue
        drift = fb[np.argmax(sb[p + 5])] / max(fb[np.argmax(sb[p + 1])], 1.0)
        e = _db((sb[p:p + 20] ** 2).sum(1))
        fall = np.nonzero(e < e.max() - 10)[0]
        dur = (fall[0] if len(fall) else 20) / FPS
        (kicks if (drift < 0.95 or dur < 0.16) else notes).append(p / FPS)
    F["kick_t"] = np.array(kicks, np.float32)                   # удары бочки
    F["low_t"] = np.array(sorted(kicks + notes), np.float32)    # все атаки в низу (бочка + ноты баса)

    # ---- голос: YIN на полосе 180–1500 Гц (выше баса), 8 кГц, шаг 20 мс
    sos = signal.butter(4, [180, 1500], "bandpass", fs=sr, output="sos")
    # АЧХ как у filtfilt (два прохода), понижение до 8 кГц — потоково, без копий всего трека
    y8 = spec.down_stream(lambda a, b: y[a:b], len(y), sr // 8000, sos, passes=2)
    f0, ap, rms = _yin(y8)
    F.update(f0=f0, ap=ap, vrms=rms, vfps=50.0)
    return F


def metrics(F: dict, t0: float, t1: float) -> dict:
    """Сводка по куску t0..t1 (секунды)."""
    fps = F["fps"]
    a, b = int(t0 * fps), max(int(t1 * fps), int(t0 * fps) + 4)
    d = max(t1 - t0, 1e-3)
    T = float(np.median(F["tot"][a:b]))
    m = dict(
        mid=float(np.median(F["mid"][a:b]) - T),
        hi=float(np.median(F["hi"][a:b]) - T),
        hlow=float(np.median(F["hlow"][a:b]) - T) if a < len(F["hlow"]) else -60.0,
        phi=float(np.percentile(F["sn"][a:b] - T, 90)),
        hit=float(np.percentile(F["sn"][a:b] - T, 99)),
        hitjump=float(np.percentile(F["sn"][a:b], 99) - np.percentile(F["sn"][a:b], 50)),
        kick=float(((F["kick_t"] >= t0) & (F["kick_t"] < t1)).sum() / d),
        lowev=float(((F["low_t"] >= t0) & (F["low_t"] < t1)).sum() / d),
        sustain=float(np.median(F["hmid"][a:b] - F["pmid"][a:b])),
    )
    h = F["hon"][a:b]
    pk, _ = signal.find_peaks(h, height=max(1.5, 2.5 * float(np.median(F["hon"]))), distance=int(0.08 * fps))
    m["notes"] = len(pk) / d
    sn = np.minimum(F["sb"][a:b], F["sn"][a:b] + 6) - T
    pk, _ = signal.find_peaks(sn, height=-14, prominence=8, distance=int(0.1 * fps))
    m["snare"] = len(pk) / d
    pa, pb_ = int(t0 * F["pitched_fps"]), max(int(t1 * F["pitched_fps"]), int(t0 * F["pitched_fps"]) + 1)
    m["pitched"] = float(F["pitched"][pa:pb_].mean()) if pb_ > pa else 0.0
    va, vb = int(t0 * F["vfps"]), max(int(t1 * F["vfps"]), int(t0 * F["vfps"]) + 2)
    rm = F["vrms"][va:vb]
    if len(rm):
        v = (F["ap"][va:vb] < 0.25) & (rm > np.percentile(rm, 20)) & (F["f0"][va:vb] > 160)
        m["voiced"] = float(v.mean())
    else:
        m["voiced"] = 0.0
    return m


def grade(m: dict, kind: str, th: dict = TH) -> int:
    """2 — «чисто», 1 — «с примесью», 0 — не подходит."""
    if m["kick"] >= th["kick_veto"]:
        return 0
    if kind == "bass":
        if m["hlow"] < th["bass_hlow"] or m["snare"] >= th["snare_veto"]:
            return 0
        if m["hit"] > th["bass_hit_veto"] and m["hitjump"] > th["bass_hit_jump"]:
            return 0                      # громкий резкий удар в верхах — это бочка/малый, а не голый бас
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
            return 0                      # громкий ударный верх при живом низе — это бит, а не пэд
        clean = m["notes"] <= th["pads_notes1"] and m["snare"] < th["snare_veto"] and m["voiced"] < th["pads_voc1"]
        return 2 if clean else 1
    return 0


def score(m: dict, kind: str) -> float:
    """Насколько «изолированно» звучит кусок (0..1) — для выбора лучших среди подходящих."""
    if kind == "bass":
        return float(np.clip((-m["mid"] - 10) / 30, 0, 1) * 0.5 + np.clip((-m["hi"] - 25) / 30, 0, 1) * 0.5
                     - 0.3 * min(1.0, m["kick"]))
    if kind == "vocal":
        return float(np.clip(m["voiced"] / 0.6, 0, 1) * 0.7 + np.clip((-m["hlow"] - 5) / 30, 0, 1) * 0.3)
    if kind == "pads":
        return float(np.clip(m["pitched"], 0, 1) * 0.4 + np.clip(1 - m["voiced"] / 0.3, 0, 1) * 0.3
                     + np.clip((-m["phi"] - 20) / 25, 0, 1) * 0.3)
    return 0.0
