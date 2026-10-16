"""
Процессы для анализа треков (конвейер).

Analyzer — пул процессов: пока сеть скачивает следующий трек, предыдущие анализируются
параллельно на других ядрах. Процессов — ядра минус одно (одно остаётся окну и плееру),
но не больше трёх и не больше, чем позволяет память. Процессы работают с пониженным
приоритетом и ограниченным числом потоков FFT/BLAS, чтобы не мешать друг другу и джему.
Если процессы почему-то не запускаются или падают — анализ идёт прямо в вызывающем
потоке, как в версиях до 1.2.
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from .i18n import T

MAX_WORKERS = 3
GB_PER_WORKER = 2.5          # с большим запасом: пик одного анализа ~0.2–0.35 ГБ, остальное — системе и браузеру
MAX_CRASHES = 2              # после стольких падений пула — работаем в одном потоке


def total_ram_gb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2 ** 30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except Exception:
        return None


def worker_count(tasks: int | None = None) -> int:
    """Сколько процессов анализа запускать (CRATEDIGGER_WORKERS=0 — без процессов, в одном потоке)."""
    env = os.environ.get("CRATEDIGGER_WORKERS", "").strip()
    if env.isdigit():
        return int(env) if not tasks else min(int(env), max(1, tasks))
    n = max(1, min(MAX_WORKERS, (os.cpu_count() or 1) - 1))
    ram = total_ram_gb()
    if ram:
        n = max(1, min(n, int(ram // GB_PER_WORKER)))
    if tasks:
        n = max(1, min(n, tasks))
    return n


def _init_worker(threads: int):
    """Старт процесса анализа: пониженный приоритет и свой лимит потоков FFT."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)       # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:
        pass
    from . import spec, core  # noqa: F401  (numpy/scipy грузятся сразу, а не на первом треке)
    spec.FFT_WORKERS = threads


def _ping() -> int:
    return os.getpid()


class Analyzer:
    """Пул процессов анализа с запасным режимом «в этом же потоке»."""

    def __init__(self, log, tasks: int | None = None, tmp: Path | None = None):
        self.log = log
        self.n = worker_count(tasks)
        self.tmp = tmp
        self.ex: ProcessPoolExecutor | None = None
        self.crashes = 0
        self._clean_tmp()                         # хвосты прошлого «Стопа» — временный звук
        self._start()

    def _clean_tmp(self):
        if not self.tmp:
            return
        try:
            Path(self.tmp).mkdir(parents=True, exist_ok=True)
            for f in Path(self.tmp).glob("an_*.s16"):
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    @property
    def parallel(self) -> bool:
        return self.ex is not None

    def _start(self):
        if self.n <= 0:                         # CRATEDIGGER_WORKERS=0 — как до 1.2, в одном потоке
            self.n = 1
            return
        try:
            import multiprocessing as mp
            threads = max(1, (os.cpu_count() or 1) // self.n)
            for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
                os.environ[v] = str(threads)                   # действует только на новые процессы
            kw = dict(max_workers=self.n, mp_context=mp.get_context("spawn"),
                      initializer=_init_worker, initargs=(threads,))
            if sys.version_info >= (3, 11):
                kw["max_tasks_per_child"] = 8                  # процесс обновляется — память не копится
            self.ex = ProcessPoolExecutor(**kw)
            for _ in range(self.n):                            # процессы стартуют сразу, пока идёт поиск
                self.ex.submit(_ping)
        except Exception as e:
            self.ex = None
            self.log(T("⚠ Параллельный анализ недоступен ({e}) — работаю в одном потоке", e=str(e)[:120]))

    def submit(self, fn, *args) -> Future:
        """Отдать задачу в процесс; если пул сломан — пересоздать, а после частых падений
        выполнить прямо здесь (как раньше)."""
        if self.ex is not None:
            try:
                return self.ex.submit(fn, *args)
            except (BrokenProcessPool, RuntimeError):
                self.crashes += 1
                self._drop()
                if self.crashes < MAX_CRASHES:
                    self.log(T("⚠ Процесс анализа упал — перезапускаю"))
                    self._start()
                    if self.ex is not None:
                        return self.submit(fn, *args)
                else:
                    self.log(T("⚠ Процессы анализа падают — дальше работаю в одном потоке"))
        f: Future = Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:   # noqa: BLE001 — ошибку отдаём вызывающему через Future
            f.set_exception(e)
        return f

    def _drop(self):
        ex, self.ex = self.ex, None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def kill(self):
        """«Стоп»: сразу прибить процессы (незаконченные анализы бросаем)."""
        ex, self.ex = self.ex, None
        if ex is None:
            return
        procs = list((getattr(ex, "_processes", None) or {}).values())
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        t = time.time() + 3
        for p in procs:
            try:
                p.join(max(0.0, t - time.time()))
            except Exception:
                pass
        self._clean_tmp()                         # временный звук прибитых анализов

    def close(self):
        """Обычное завершение: дождаться процессов и освободить память."""
        ex, self.ex = self.ex, None
        if ex is not None:
            ex.shutdown(wait=True)
