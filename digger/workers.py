"""
Processes for track analysis (the pipeline).

Analyzer is a process pool: while the network downloads the next track, the previous ones are
analysed in parallel on other cores. The number of processes is cores minus one (one stays for the
window and the player), but no more than three and no more than memory allows. The processes run at
a lower priority and with a limited number of FFT/BLAS threads, so they don't disturb each other or
the jam. If the processes fail to start or keep crashing, analysis runs right in the calling thread,
as in versions before 1.2.
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
GB_PER_WORKER = 2.5          # very generous: one analysis peaks at ~0.2-0.35 GB, the rest is for the OS and browser
MAX_CRASHES = 2              # after this many pool crashes we switch to a single thread


def total_ram_gb() -> float | None:
    """Physical memory of the computer in GB (None if it can't be determined)."""
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
    """How many analysis processes to start (CRATEDIGGER_WORKERS=0 — no processes, single thread)."""
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
    """Start of an analysis process: lower priority and its own FFT thread limit."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)       # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:
        pass
    from . import spec, core  # noqa: F401  (numpy/scipy load now, not on the first track)
    spec.FFT_WORKERS = threads


def _ping() -> int:
    """No-op task used to make the processes start right away (while the search is running)."""
    return os.getpid()


class Analyzer:
    """Pool of analysis processes with a fallback mode "in this same thread"."""

    def __init__(self, log, tasks: int | None = None, tmp: Path | None = None):
        self.log = log
        self.n = worker_count(tasks)
        self.tmp = tmp
        self.ex: ProcessPoolExecutor | None = None
        self.crashes = 0
        self._clean_tmp()                         # leftovers of a previous "Stop" — temporary audio
        self._start()

    def _clean_tmp(self):
        """Delete temporary decoded audio files (an_*.s16) left in the tmp folder."""
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
        """Create the process pool (or stay in single-thread mode if that's impossible)."""
        if self.n <= 0:                         # CRATEDIGGER_WORKERS=0 — like before 1.2, single thread
            self.n = 1
            return
        try:
            import multiprocessing as mp
            threads = max(1, (os.cpu_count() or 1) // self.n)
            for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
                os.environ[v] = str(threads)                   # only affects newly started processes
            kw = dict(max_workers=self.n, mp_context=mp.get_context("spawn"),
                      initializer=_init_worker, initargs=(threads,))
            if sys.version_info >= (3, 11):
                kw["max_tasks_per_child"] = 8                  # processes get recycled — memory doesn't pile up
            self.ex = ProcessPoolExecutor(**kw)
            for _ in range(self.n):                            # processes start now, while the search is running
                self.ex.submit(_ping)
        except Exception as e:
            self.ex = None
            self.log(T("⚠ Parallel analysis unavailable ({e}) — working in a single thread", e=str(e)[:120]))

    def submit(self, fn, *args) -> Future:
        """Hand a task to a process; if the pool is broken, recreate it, and after frequent crashes
        run the task right here (as before)."""
        if self.ex is not None:
            try:
                return self.ex.submit(fn, *args)
            except (BrokenProcessPool, RuntimeError):
                self.crashes += 1
                self._drop()
                if self.crashes < MAX_CRASHES:
                    self.log(T("⚠ An analysis process crashed — restarting"))
                    self._start()
                    if self.ex is not None:
                        return self.submit(fn, *args)
                else:
                    self.log(T("⚠ Analysis processes keep crashing — continuing in a single thread"))
        f: Future = Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:   # noqa: BLE001 — the error goes to the caller through the Future
            f.set_exception(e)
        return f

    def _drop(self):
        """Forget a broken pool without waiting for it."""
        ex, self.ex = self.ex, None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def kill(self):
        """"Stop": kill the processes immediately (unfinished analyses are abandoned)."""
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
        self._clean_tmp()                         # temporary audio of the killed analyses

    def close(self):
        """Normal shutdown: wait for the processes and free their memory."""
        ex, self.ex = self.ex, None
        if ex is not None:
            ex.shutdown(wait=True)
