"""
Searching YouTube Music for records and downloading their audio.
ytmusicapi -> search (with fallbacks through yt-dlp), yt-dlp -> download.

All network requests go strictly one at a time with random pauses between them (PAUSE), so the
traffic looks like a person listening, not a bot. "Stop" interrupts any request at once (Stopped).
"""
from __future__ import annotations

import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from .dsp import ffmpeg_exe
from .i18n import T

# "The crate": genres where boom bap samples are usually dug (used when no tags are given)
CRATE = [
    "soul", "funk", "jazz funk", "rare groove", "northern soul", "deep funk", "psychedelic soul",
    "library music", "soundtrack", "bossa nova", "MPB brazil", "samba soul", "afrobeat",
    "ethiopian jazz", "gospel", "spiritual jazz", "soul jazz", "jazz fusion", "quiet storm",
    "sweet soul", "blaxploitation", "italian library", "french library", "turkish psych",
    "bollywood", "city pop", "latin soul", "boogaloo", "exotica", "easy listening", "lounge",
    "organ jazz", "rhodes", "harp jazz", "orchestral soul", "chicano soul", "disco funk",
    "modal jazz", "hard bop", "vocal jazz", "girl group", "doo wop", "psych rock", "prog rock",
    "folk", "reggae", "rocksteady", "dub", "highlife", "calypso", "jazz waltz", "philly soul",
    "chicago soul", "memphis soul", "new orleans funk", "boogie", "new jack swing", "acid jazz",
    "jazz piano", "vibraphone jazz", "flute jazz", "cinematic strings", "french pop", "yé-yé",
    "italian soundtrack", "spaghetti western", "brazilian jazz", "japanese jazz", "korean psych",
    "african funk", "cumbia", "salsa", "smooth jazz", "r&b ballad",
]
FLAVOR = ["", "", "", "rare", "obscure", "instrumental", "groove", "breaks", "deep", "original"]

BAD = re.compile(
    r"type beat|lo-?fi|\bremix|slowed|sped up|nightcore|\b8d\b|karaoke|tutorial|reaction|sample pack|"
    r"bass boosted|\b1 hour|full album|playlist|\bmix 20\d\d|ai cover|drumless|backing track|"
    r"\bfree beat|\bbeat tape|\bmashup",
    re.I,
)
REISSUE = re.compile(r"remaster|reissue|anniversary|deluxe|expanded|collection|best of|greatest|"
                     r"anthology|classics|essential|original|vintage|rare|compilation|hits|story|years",
                     re.I)
YEAR_RX = re.compile(r"(?<!\d)(19[4-9]\d|20[0-3]\d)(?!\d)")
DECADE_RX = re.compile(r"(?<!\d)(19)?[4-9]0\s*'?\s*(s|х|x|-е|е)\b|(?<!\d)19[4-9]\d(?!\d)", re.I)


# --------------------------------------------------------------------------- search queries
ONLY_HINTS = {          # words added to the query for "Only ..." (with several selected — one at random)
    "drums": ["drum break", "breakbeat", "drum intro", "drum solo", "breaks", "funky drums"],
    "bass": ["bass intro", "bass break", "bass solo", "funky bass", "bassline", "bass groove"],
    "vocal": ["a cappella", "vocal intro", "acapella", "gospel vocal", "vocal only", "chant"],
    "pads": ["ambient", "ambient intro", "atmospheric", "library ambient", "strings intro", "new age", "breakdown"],
}
PAUSE = (0.4, 1.2)       # random pause between requests to YouTube, seconds
RARE_HINTS = ["rare", "obscure", "private press", "deep", "unknown", "lost", "library", "promo"]


def _extra(rng, only, rare) -> list[str]:
    """Optional extra words for the query: a hint for the selected "Only" type and/or a "rare" word."""
    extra = []
    if only and rng.random() < 0.65:
        extra.append(rng.choice(ONLY_HINTS[rng.choice(list(only))]))
    if rare and rng.random() < 0.5:
        extra.append(rng.choice(RARE_HINTS))
    return extra


def build_query(tags: str, y0: int | None, y1: int | None, rng: random.Random, only=(), rare=False) -> str:
    """A search query from the user's tags (or a random genre of the crate) plus, sometimes, a year
    or decade and extra words. y0/y1 = None — years are not filtered and not added to the query.
    only — the selected "Only ..." types."""
    tags = (tags or "").strip()
    if y0 is None or y1 is None:
        extra = _extra(rng, only, rare)
        base = tags or rng.choice(CRATE)
        # without a year, vary the queries with "seasoning" words, otherwise the results would repeat
        fl = " ".join(extra) or rng.choice(FLAVOR + ["b-side", "album track", "deep cut", "groove", "session"])
        return " ".join(v for v in (base, fl) if v)
    year = rng.randint(y0, y1)
    ex = " ".join(_extra(rng, only, rare))
    if tags:
        if DECADE_RX.search(tags):
            return f"{tags} {ex}".strip()
        r = rng.random()
        if r < 0.55:
            return " ".join(v for v in (tags, ex, str(year)) if v)
        if r < 0.85:
            return " ".join(v for v in (tags, ex, f"{year // 10 * 10}s") if v)
        return " ".join(v for v in (tags, ex or rng.choice(FLAVOR), str(year)) if v)
    g = rng.choice(CRATE)
    fl = ex or rng.choice(FLAVOR)
    if rng.random() < 0.7:
        return " ".join(v for v in (g, fl, str(year)) if v)
    return " ".join(v for v in (g, fl, f"{year // 10 * 10}s") if v)


def parse_count(v) -> int | None:
    """'1.2M' / '345K' / '12,345 plays' / 9876 -> a number (Russian "тыс/млн" too)"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"([\d.,]+)\s*([KMBkmb]|тыс|млн|млрд)?", str(v).replace("\u00a0", " "))
    if not m:
        return None
    num = m.group(1)
    mult = {"k": 1e3, "тыс": 1e3, "m": 1e6, "млн": 1e6, "b": 1e9, "млрд": 1e9}.get((m.group(2) or "").lower(), 1)
    try:
        if mult == 1:
            return int(num.replace(",", "").replace(".", ""))
        return int(float(num.replace(",", ".")) * mult)
    except ValueError:
        return None


def human_count(n: int | None) -> str:
    """A play count for people: 1.2M, 345K (in the interface language)."""
    if n is None:
        return ""
    if n >= 1_000_000:
        return T("{n}M", n=f"{n / 1e6:.1f}".removesuffix(".0"))
    if n >= 1000:
        return T("{n}K", n=n // 1000)
    return str(n)


# --------------------------------------------------------------------------- yt-dlp
class _Log:
    """Logger for yt-dlp: passes only the warnings that matter for the user into our log."""
    def __init__(self, log):
        self.log = log

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        m = ANSI.sub("", str(msg))
        if any(s in m for s in ("Sign in", "bot", "JavaScript", "js runtime", "PO Token", "n challenge")):
            self.log("⚠ yt-dlp: " + m[:220])

    def error(self, msg):
        m = ANSI.sub("", str(msg))
        if is_bot_error(m) or "cookies" in m.lower():
            return            # we handle these ourselves and explain them in plain words
        self.log("✕ yt-dlp: " + m[:260])


def js_runtimes():
    """A JavaScript runtime for yt-dlp — YouTube requires one since November 2025 (Deno preferred)."""
    try:
        import deno  # pip install deno
        p = deno.find_deno_bin()
        if p and os.path.exists(p):
            return {"deno": {"path": str(p)}}
    except Exception:
        pass
    for name, key in (("deno", "deno"), ("node", "node"), ("qjs", "quickjs")):
        p = shutil.which(name)
        if p:
            return {key: {"path": p}}
    return None


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def is_bot_error(m: str) -> bool:
    """Is this the "confirm you're not a bot" / sign-in wall?"""
    m = str(m)
    return ("Sign in to confirm" in m or "not a bot" in m or "confirm your age" in m.lower()
            or "LOGIN_REQUIRED" in m)


def is_cookie_read_error(m: str) -> bool:
    """Did reading a browser's cookies fail (browser missing, locked database, no access)?"""
    m = str(m).lower()
    return any(s in m for s in ("could not find", "cookie", "decrypt", "database is locked",
                                "keyring", "permission denied", "no such file", "unsupported browser"))


class Stopped(BaseException):
    """"Stop" was pressed — the network request is abandoned at once. Derived from BaseException so
    that ordinary `except Exception` blocks (ours or inside yt-dlp) don't swallow it."""


class BotBlocked(RuntimeError):
    """YouTube refuses even with cookies — there is no point in digging further."""


# the order in which browsers are tried when YouTube asks for cookies
if sys.platform == "darwin":
    COOKIE_BROWSERS = ["firefox", "safari", "chrome", "brave", "edge"]
elif os.name == "nt":
    COOKIE_BROWSERS = ["firefox", "edge", "chrome", "brave", "opera"]
else:
    COOKIE_BROWSERS = ["firefox", "chrome", "chromium", "brave"]


def explain_error(e: Exception) -> str:
    """A download/search error in plain words for the log."""
    m = ANSI.sub("", str(e))
    if isinstance(e, BotBlocked):
        return str(e)
    if is_bot_error(m):
        return T("YouTube asks you to confirm you're not a bot.")
    if "429" in m or "Too Many Requests" in m:
        return T("YouTube has temporarily limited requests (429). Wait 10–30 minutes.")
    if "Private video" in m or "unavailable" in m.lower():
        return T("The track is unavailable in your region or removed — taking the next one.")
    if "ffmpeg" in m.lower():
        return T("ffmpeg problem: {e}", e=m[:200])
    return m[:300]


class Fetcher:
    """Everything that talks to YouTube: search, probing a track's metadata, downloading audio.
    One instance per dig; requests are paced (_pace) and interruptible (_run + stop)."""
    def __init__(self, tmp_dir: Path, log, cookies_browser: str | None = None, cookie_file: Path | None = None,
                 stop=None):
        import yt_dlp  # noqa: F401  (check that it is installed)
        self.stop = stop or (lambda: False)   # has "Stop" been pressed?
        self.tmp = Path(tmp_dir)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.cookies = (cookies_browser or "").strip().lower() or None
        self.cookie_file = str(cookie_file) if cookie_file and Path(cookie_file).exists() else None
        if self.cookie_file:
            log(T("🍪 Using cookies.txt from the program folder"))
        self.found_browser = None       # the browser whose cookies worked
        self._bad_browsers: set[str] = set()
        self._ytm = None
        self._last = 0.0                # when the previous request finished
        self.js = js_runtimes()
        if not self.js:
            log(T("⚠ Deno/Node not found — YouTube may refuse to give audio. Run the start file again."))

    def _opts(self, **extra):
        """Options for yt_dlp.YoutubeDL (cookies, JS runtime, output template, pauses, stop hook)."""
        o = dict(
            quiet=True, no_warnings=False, noprogress=True, noplaylist=True,
            retries=5, fragment_retries=5, extractor_retries=3, socket_timeout=25,
            ffmpeg_location=ffmpeg_exe(), logger=_Log(self.log),
            remote_components=["ejs:github"],
            outtmpl=str(self.tmp / "%(id)s.%(ext)s"),
            format="bestaudio/best",
            overwrites=True, cachedir=str(self.tmp / ".ytdlp-cache"),
            # don't hammer YouTube with back-to-back requests — like a normal listener, without a steady "robot rhythm"
            sleep_interval_requests=round(random.uniform(*PAUSE), 2), sleep_interval=1, max_sleep_interval=3,
        )
        o["progress_hooks"] = [self._hook]    # "Stop" breaks the download at the next chunk
        if self.js:
            o["js_runtimes"] = self.js
        if self.cookie_file:
            o["cookiefile"] = self.cookie_file
        elif self.cookies:
            o["cookiesfrombrowser"] = (self.cookies,)
        o.update(extra)
        return o

    def _hook(self, d):
        """yt-dlp progress hook: raising here aborts the download."""
        if self.stop():
            raise Stopped()

    def _pace(self):
        """Random pause between requests (0.4-1.2 s after the previous one ended); "Stop" interrupts it."""
        end = self._last + random.uniform(*PAUSE)
        while time.monotonic() < end:
            if self.stop():
                raise Stopped()
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        if self.stop():
            raise Stopped()

    def _run(self, fn):
        """Run a request in a separate thread: wait for the answer, but on "Stop" return at once without
        waiting for YouTube (the abandoned request finishes on its own and writes nothing)."""
        box = {}

        def work():
            try:
                box["r"] = fn()
            except BaseException as e:   # noqa: BLE001 — hand the error over to the caller
                box["e"] = e

        th = threading.Thread(target=work, daemon=True, name="net")
        th.start()
        while th.is_alive():
            th.join(0.05)
            if self.stop():
                raise Stopped()
        if "e" in box:
            raise box["e"]
        return box["r"]

    def _done(self):
        """Remember when a request finished (the next pause counts from here)."""
        self._last = time.monotonic()

    # ------------------------------------------------------------- search
    def search(self, query: str, limit: int = 40) -> list[dict]:
        """Search YouTube Music for songs. -> [dict(id, title, artist, album, duration, year, views)]"""
        self._pace()
        try:
            return self._run(lambda: self._search(query, limit))
        finally:
            self._done()

    def _search(self, query: str, limit: int) -> list[dict]:
        """Try the search methods one by one until one returns results."""
        errors = []
        for fn in (self._search_ytmusicapi, self._search_ytdlp_music, self._search_ytdlp_plain):
            try:
                res = fn(query, limit)
                if res:
                    return res
            except Exception as e:  # try the next method
                errors.append(f"{fn.__name__}: {str(e)[:120]}")
        if errors:
            self.log(T("⚠ search: {e}", e=" | ".join(errors)))
        return []

    def _search_ytmusicapi(self, query, limit):
        from ytmusicapi import YTMusic
        if self._ytm is None:
            self._ytm = YTMusic(language="en")
        out = []
        for r in self._ytm.search(query, filter="songs", limit=limit) or []:
            vid = r.get("videoId")
            if not vid:
                continue
            dur = r.get("duration_seconds")
            if not dur and r.get("duration"):
                try:
                    p = [int(v) for v in str(r["duration"]).split(":")]
                    dur = sum(v * 60 ** i for i, v in enumerate(reversed(p)))
                except Exception:
                    dur = None
            out.append(dict(
                id=vid, title=r.get("title") or "",
                artist=", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name")),
                album=(r.get("album") or {}).get("name") or "", duration=dur,
                year=r.get("year"), query=query, views=parse_count(r.get("views"))))
        return out

    def _flat(self, url, limit):
        """A search results page through yt-dlp without resolving each entry (fast)."""
        import yt_dlp
        with yt_dlp.YoutubeDL(self._opts(extract_flat="in_playlist", playlistend=limit,
                                         noplaylist=False)) as y:
            info = y.extract_info(url, download=False)
        out = []
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id"):
                continue
            out.append(dict(id=e["id"], title=e.get("title") or "",
                            artist=e.get("artist") or e.get("channel") or e.get("uploader") or "",
                            album=e.get("album") or "", duration=e.get("duration"), year=None,
                            views=parse_count(e.get("view_count"))))
        return out

    def _search_ytdlp_music(self, query, limit):
        res = self._flat("https://music.youtube.com/search?q=" + urllib.parse.quote_plus(query) + "#songs", limit)
        for r in res:
            r["query"] = query
        return res

    def _search_ytdlp_plain(self, query, limit):
        res = self._flat(f"ytsearch{min(limit, 30)}:{query} official audio", limit)
        for r in res:
            r["query"] = query
        return res

    # ------------------------------------------------------------- probing + downloading
    def probe(self, cand: dict) -> dict:
        """Full metadata of a track (title, artist, duration, description with the year) without downloading."""
        self._pace()
        try:
            return self._run(lambda: self._with_cookie_fallback(lambda: self._probe(cand)))
        finally:
            self._done()

    def download(self, cand: dict, info: dict | None = None) -> Path:
        """Download the best audio stream of a track into the tmp folder. -> path of the file"""
        self._pace()

        def job():
            try:
                try:
                    return self._download(cand, info)
                except Exception as e:
                    if not is_bot_error(e):
                        raise
                # the stream links in info may have been fetched without cookies — get everything again
                return self._with_cookie_fallback(lambda: self._download(cand, None))
            except Stopped:
                self._drop_partial(cand["id"])    # don't leave a half-downloaded file behind
                raise
        try:
            return self._run(job)
        finally:
            self._done()

    def _drop_partial(self, vid: str):
        """Delete all temporary files of this video id."""
        for f in self.tmp.glob(f"{vid}.*"):
            try:
                f.unlink()
            except OSError:
                pass

    def _with_cookie_fallback(self, fn):
        """YouTube asks "confirm you're not a bot" -> try the cookies of browsers where you have used
        YouTube (as if the request came from your browser). The first browser that works is kept."""
        try:
            return fn()
        except Exception as e:
            if not is_bot_error(e):
                raise
            first = e
        if self.cookie_file:
            raise BotBlocked(T(
                "YouTube doesn't accept cookies.txt — the file seems outdated. Export it again (sign "
                "in to YouTube in the browser) or delete cookies.txt from the program folder.")) from first
        if self.cookies:
            self._bad_browsers.add(self.cookies)
        tried_any = False
        for b in COOKIE_BROWSERS:
            if b in self._bad_browsers:
                continue
            if self.stop():
                raise Stopped()
            self.cookies = b
            self.log(T("🍪 YouTube asks for confirmation — taking cookies from {b}…", b=b.capitalize()))
            try:
                res = fn()
                self.found_browser = b
                self.log(T("   ✓ it worked: digging on with {b} cookies (saved in settings)", b=b.capitalize()))
                return res
            except Exception as e2:
                m = ANSI.sub("", str(e2))
                self._bad_browsers.add(b)
                if is_bot_error(m):
                    tried_any = True
                    self.log(T("   ✕ YouTube still refuses even with {b} cookies", b=b.capitalize()))
                elif is_cookie_read_error(m):
                    self.log(T("   – {b}: no cookies found (browser not installed or access denied)", b=b.capitalize()))
                else:
                    raise
        self.cookies = None
        if tried_any:
            raise BotBlocked(T(
                "YouTube refuses even with browser cookies. Open music.youtube.com in Firefox, sign "
                "in to your Google account (the “Sign in” button at the top right), play any track "
                "for a couple of seconds, then press “Dig” again. If that doesn't help, wait 30–60 "
                "minutes: YouTube has temporarily limited your IP.")) from first
        raise BotBlocked(T(
            "YouTube asks you to confirm you're not a bot, and browser cookies couldn't be read. Open "
            "music.youtube.com in Firefox, sign in and press “Dig” again. Plan B: put a cookies.txt "
            "file into the program folder (see README).")) from first

    def _probe(self, cand: dict) -> dict:
        import yt_dlp
        url = f"https://music.youtube.com/watch?v={cand['id']}"
        with yt_dlp.YoutubeDL(self._opts()) as y:
            return y.extract_info(url, download=False)

    def _download(self, cand: dict, info: dict | None = None) -> Path:
        import yt_dlp
        url = f"https://music.youtube.com/watch?v={cand['id']}"
        for f in self.tmp.glob(f"{cand['id']}.*"):
            try:
                f.unlink()
            except OSError:
                pass
        with yt_dlp.YoutubeDL(self._opts()) as y:
            done = None
            if info is not None:
                try:
                    done = y.process_ie_result(info, download=True)
                except Exception:
                    done = None
            if done is None:
                done = y.extract_info(url, download=True)
        path = None
        for d in (done or {}).get("requested_downloads") or []:
            if d.get("filepath") and os.path.exists(d["filepath"]):
                path = d["filepath"]
        if not path:
            files = [p for p in self.tmp.glob(f"{cand['id']}.*") if not p.name.endswith((".part", ".ytdl"))]
            if not files:
                raise RuntimeError(T("the file didn't download"))
            path = str(max(files, key=lambda p: p.stat().st_size))
        return Path(path)


# --------------------------------------------------------------------------- metadata
def era_check(cand: dict, info: dict, y0: int, y1: int, strict: bool):
    """Check that the recording really is from the wanted era (reissues of old recordings pass).
    -> (ok, year or None, explanation)"""
    desc = info.get("description") or ""
    text = " ".join(str(v) for v in (cand.get("title"), cand.get("album"), info.get("title"),
                                     info.get("album")) if v)
    m = re.search(r"℗\s*(\d{4})", desc)
    p_year = int(m.group(1)) if m else None
    rel = info.get("release_year")
    if not rel:
        mm = re.search(r"Released on:\s*(\d{4})", desc)
        rel = int(mm.group(1)) if mm else None
    txt_years = [int(v) for v in YEAR_RX.findall(text)]
    if cand.get("year"):
        try:
            txt_years.append(int(str(cand["year"])[:4]))
        except ValueError:
            pass
    in_range = lambda v: v is not None and y0 <= v <= y1

    # ℗ — the year of the sound recording; reissues usually keep the original one
    if p_year and in_range(p_year):
        return True, p_year, "℗"
    if any(in_range(v) for v in txt_years):
        return True, next(v for v in txt_years if in_range(v)), T("in the title")
    if rel and in_range(rel):
        return True, rel, T("release date")
    if strict:
        return False, rel or p_year, T("year not confirmed")
    newest = max([v for v in (p_year, rel) if v] or [0])
    if newest and newest < y0:
        return False, newest, T("too old")
    if newest and newest > y1:
        # a reissue of an old recording — let it through if there is a hint of that
        if REISSUE.search(text) or REISSUE.search(desc[:600]):
            return True, None, T("reissue")
        return False, newest, T("recorded {y}", y=newest)
    return True, None, T("year unknown")


def basic_filter(cand: dict, min_s=95, max_s=600) -> str | None:
    """Quick rejection by title and length (remixes, lo-fi, type beats, hour-long mixes). -> reason or None"""
    t = f"{cand.get('title', '')} {cand.get('album', '')} {cand.get('artist', '')}"
    if BAD.search(t):
        return T("not it (remix/lo-fi/beat)")
    d = cand.get("duration")
    if d and (d < min_s or d > max_s):
        return T("wrong length")
    return None
