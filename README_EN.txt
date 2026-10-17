CRATEDIGGER 1.3.2 — sample digging for boom bap
===============================================
(Русский: README.txt · Español: README_ES.txt)

The program "digs through the crates" by itself: it finds tracks from 1960–1999 on
YouTube Music (by your tags or at random), downloads them, finds the steadiest parts
and cuts 3 loops from every track. Every loop:
  * is cut exactly on the downbeats and loops without clicks or drift:
    the boundaries sit on the real start of the hit (even with a soft attack or a flam),
    and the finished loop is checked — if the start of the next hit slipped into the end,
    the end is moved and the loop is cut again; if that fails, a drum loop is dropped
    and other loops get a soft fade at the end;
  * is fitted to a tempo that is a multiple of 5 BPM (60, 65 … 120);
  * is named by key and tempo, for example  Gmaj110Quark1.wav


LAUNCH
------
Windows:  double-click START_WINDOWS.bat
          (if Windows says "protected your PC" — "More info" → "Run anyway")
macOS:    right-click START_MAC.command → "Open" → "Open"
          (like this the first time, a double-click works afterwards)
Linux:    bash START_LINUX.sh

On the first launch the program installs Python (on Windows) and the libraries it needs
into this same folder — 1–3 minutes, internet required. After that it starts in seconds.
The program window opens. Don't close the black console window while digging.
Unpack the archive into a normal folder (don't run it straight from the zip).

Interface language — the flags above the program name at the top left: English,
Russian, Spanish. The choice is remembered. The log and the credits.txt /
MODEL_SAMPLES.txt files of new crates are written in the same language.


HOW TO USE
----------
1. Type tags: genre, instrument, year, mood — for example
     soul, rhodes, 1973        jazz funk flute        italian library 70s
   You can click the ready-made tags under the field. Empty = random records.
2. Press "Dig". Records appear as soon as they are ready.
3. The ▶ button plays a loop on repeat. You can start several at once:
   the next ones come in exactly at the top of a bar and follow the tempo of the first —
   so you can hear right in the window how loops sound together.
   When nothing plays, hovering a loop outlines the loops in a compatible key.
   During a jam the outline always shows the loops compatible with the first one
   started (the master), taking into account that the next loop follows its tempo and
   shifts in pitch: e.g. under Emin 70, Fmin 75 and F#min 80 are outlined (slowed down
   they sound in Emin). Loops that end up more than 35 cents out of tune are not
   outlined. The master's key is shown in the bottom bar next to the BPM. With
   "Tempo from first loop" off nothing is pitched and the original keys are compared.
   "Download jam" (next to "Stop all") — a ZIP of the loops playing now, each pitched
   exactly as it sounds in the jam. The archive is named after the master loop
   (Gmaj95-<date>-<time>.zip), and the key and BPM in the loop names change to the
   ones after the pitch shift: Amaj80PName1 under a 95 BPM master becomes Cmaj95PName1
   (+3 semitones; Cmaj and Gmaj are Camelot neighbours). The rest of the name stays.
4. ★ — Favorites: a copy of the loop goes to the Favorites folder. Clicking again
   removes the star and the copy. Everything ever starred is collected in the
   "★ Favorites" crate — the first item in the "Add a past crate" list.
   To keep only favorites on screen: "Clear screen", then pick "★ Favorites".
   The bin removes a loop from the screen (the file stays in the folder; right after
   clicking you can "Undo").
   You can drag a loop with the mouse (by its key, name or waveform) straight into a folder
   in Explorer, Finder or onto the desktop — its WAV is copied there.
   This works in a Chrome/Edge window (that's how the program opens by itself); Firefox
   can't do it.
   The floppy disk next to the bin saves the loop's WAV to your computer when dragging
   doesn't work: Chrome/Edge ask where to save it (and remember the last folder),
   other browsers put the file into the downloads folder.
   Every new dig is added to the screen below the previous ones — everything you dug
   while the program is running stays in front of you (even after reloading the window).
   "Add a past crate" at the top brings old digs back to the screen,
   "Clear screen" removes everything from the screen (files on disk are not touched).
   "Download all as ZIP" at the bottom — all loops on screen in one archive for Model:Samples:
     90/Amin/Amin90Name1.wav
     110/G#maj/G#maj110Other2.wav
   (a BPM folder with a key folder inside; the files are the same ones that play on screen —
   stereo 24-bit if it was saved). Unpack it and transfer whole folders with
   Elektron Transfer — it converts stereo to mono by itself.
5. You can drop your own mp3/wav/flac into the window (vinyl rips, for example) —
   they are chopped the same way.
6. Re-run: hold the left mouse button on a record's cover or title in the list on the
   right and drag it into the left column (you can release anywhere in it — the column
   lights up with a blue frame). The record is cut again with the settings currently
   selected on the left: for example "Pitching" instead of "Stretching", "Only: bass"
   instead of "Any", or 1 bar instead of 2. The result appears as a separate
   "Re-run: …" crate; the old loops stay where they are.
   The source is taken from the crate folder (Sources_mp3), so the program doesn't contact
   YouTube; if it's not there, the track is downloaded again. For your own files the
   re-run works if "Keep the source track" is on in "More settings".

Useful settings:
  * "Breaks" — parts where the drums are up front; "Melody" — parts with more harmony.
  * "Only:" drums / bass / vocals / pads (for 1, 2 and 4 bar loops) — parts where only
    that plays. You can pick several at once: they work as "or" (for example drums + bass —
    each record gives both clean breaks and bare bass). Picking anything in this row
    turns off "Any / Breaks / Melody"; a click in the first row clears all "Only".
    Loops of the chosen types take turns: the type with the cleanest part goes first,
    then the types alternate (for example pads, drums, pads, drums); one type comes
    several times in a row only when the others have nothing left. Inside a type the
    clean parts come first, then the ones "with extras" — the loop caption says so
    ("bass with extras"). "Loops per record" is the total for all chosen types together.
      drums  — drums alone, no bass, chords or voice (no sounds with a series of
               overtones, the level drops almost to silence between hits);
      bass   — the bass line up front, without kick and snare (including dark,
               filtered breaks); "with extras" — with light percussion, FX,
               a chord or a voice in the background;
      vocals — a voice (one clear tone above 160 Hz) without a beat; "with extras" —
               with light bass, choir, instruments or percussion;
      pads   — sustained chords and beds without bass, drums or a lead melody
               (reverb-smeared piano is a pad too; a one-note-at-a-time melody is not).
               A low droning drone under the pad is allowed. A pad needs a tone:
               noise without harmony (crowd, applause, talk, wind) is not a pad.
               Pads with a melody on top come later in the queue.
    Vocals and pads never have a repeating kick or snare — parts with a beat are
    rejected even at slow tempos (a kick on beats 1 and 3).
    The seam of pads and vocals: if a chord or a note starts slightly before the beat
    (soft attack, reverb), the start and the end of the loop move to the real start of
    the attack — the attack stays at the start of the loop and doesn't click at the end
    (in "Pitching" this gives a little more slowdown). Vocals whose loudness at the start
    and at the end differ a lot, and off-grid pads where something new comes in during
    the last half second, are taken only if nothing else was found.
    Pads and vocals are also taken from beatless intros and from tracks with no rhythm
    at all (ambient): such a part is cut to exactly 1, 2 or 4 bars at the most common BPM
    of this dig, without stretching, with a click-free seam. In "Stretching" vocals are
    fitted to the grid as much as needed (the pitch doesn't change), in "Pitching" —
    up to ±15 %. The key in the name of drums, bass and vocals is the song's key
    (a single line can't tell the mode), for pads — the key of the loop itself.
    Such parts are rare: a record may give 1–2 loops, and the search takes longer.
  * "Look for rare ones" (checkbox under the tags field) — records with the fewest plays
    come first (up to 150K; if nothing rare turns up, the limit is raised step by step).
    The play count is shown for every record.
  * "Files to save" (in "More settings") — stereo 24-bit only by default
    (Elektron Transfer converts to mono by itself). You can add ready-made mono
    16-bit files for Model:Samples; then "Sort mono files for Model:Samples into
    folders" comes in handy too —
    ModelSamples_mono16/Amin/095 BPM/Amin95Quark1.wav etc.
  * "Stretching" changes the tempo and keeps the pitch (hits stay in place). "Pitching" —
    like the pitch fader on a turntable or an MPC: tempo and pitch change together,
    usually by less than a semitone (vocals — up to ±15 %). The key in the name is recalculated.
  * Years and BPM: the checkbox next to the label turns the filter on or off
    (both filters are off by default).
    Years off — recordings of any year are searched (modern ones included),
    BPM off — tracks are not rejected by tempo (fitting to a multiple of 5
    within 60–120 still applies). Loop length: 1, 2 or 4 bars.
  * "Debugging" (in "More settings", off by default) — every loop shows its purity and
    a 👎 wrong match button. If a loop's type is wrong (say "pads only", but it's crowd
    noise or a beat), click it: a copy of the loop and all the analyzer's readings go to
    the Feedback folder next to the crates (file feedback.jsonl). Zip that folder and send
    it — it makes improving detection easy. A second click removes the mark.
    Hover a loop's caption to see the detailed readings.


SPEED AND MEMORY
----------------
Digging runs as a pipeline: while one track downloads, the ones already downloaded are
analysed in parallel in separate processes. The number of processes is CPU cores minus one
(one is left for the window and the player so the jam doesn't stutter), but no more than three
and fewer if the computer is short on memory; they run at a lower priority. One analysis
takes about 0.2–0.35 GB of memory. Requests to YouTube still go strictly one at a time,
with random pauses, and the program doesn't download more than it needs.
On a bot check or "too many requests" the network stops at once, and records that are
already downloaded are finished. "Stop" interrupts everything at once.
Your own files and re-runs are processed in parallel too. If the processes fail to start
or crash for some reason, the program switches to working in a single thread by itself.

WHAT YOU GET
------------
Music/CrateDigger/<date>_<tags>/
  Stereo_24bit/          loops for a DAW (48 kHz, 24-bit, stereo)
  ModelSamples_mono16/   the same loops in mono 16-bit (if enabled in "More settings")
  Sources_mp3/           the full source tracks (re-runs work from them)
  Chops/                 each loop cut into 8 parts (if enabled)
  MODEL_SAMPLES.txt      table: tempo, bars, steps for each loop
  credits.txt            where each loop comes from (artist, link, time in the track)
Music/CrateDigger/Feedback/   loops marked 👎 in Debugging mode, and feedback.jsonl

File name: key + BPM + title + loop number:  Amin95Quark2.wav
Loops from "Only …" have a type letter after the BPM: D — drums, B — bass,
V — vocals, P — pads:  Amin95BQuark1.wav, Cmin80DKyoto2.wav
Loops are saved as WAV, not mp3: mp3 always adds silence at the start and end,
and the loop stops being tight.


MODEL:SAMPLES — JAMMING
-----------------------
1. Transfer the loops with Elektron Transfer (easiest — "Download all as ZIP"
   and whole folders; it converts stereo to mono by itself).
2. Pattern tempo = the number in the file name.
3. Track length = the number of steps from MODEL_SAMPLES.txt (2 bars = 32 steps), trig on step 1.
   The loop restarts every cycle and never drifts.
4. The loops are exactly bar-length, so the START knob cuts them into equal parts
   (0, 16, 32 … 112 ≈ eighths). Different START values on different steps = re-chopping, like on an MPC.
5. For jamming it helps to narrow the BPM (for example 90–90) — then all loops
   fit into one project without changing tempo.
6. The key and the Camelot code (8A, 10B…) hint at what goes together:
   the same number or a neighbouring one (8A + 8B, 8A + 9A, 8A + 7A).


IF SOMETHING DOESN'T WORK
-------------------------
* "Confirm you're not a bot". The program takes cookies from Firefox by itself
  (if it's not installed — from Edge/Chrome/Safari) and retries the same track: to YouTube
  the request looks as if it came from your browser. The browser that worked
  is remembered. If YouTube still doesn't let you in:
    1) open music.youtube.com in Firefox and sign in to your Google account
       (the "Sign in" button at the top right), play any track for a couple of seconds;
    2) press "Dig" again.
  Plan B: the "cookies.txt" extension for Firefox → on the music.youtube.com page
  click it and save the file as cookies.txt right into the program folder
  (next to README). The program picks it up by itself.
  If nothing helps — YouTube has temporarily limited your IP, wait an hour.
* "429 / too many requests" — wait 10–30 minutes.
* Nothing is found — change the tags, widen the years, turn off the strict year check.
* The window didn't open — open the address from the black window in a browser (http://127.0.0.1:8765).
* Full reinstall: delete the .venv folder next to the program and launch it again.
* yt-dlp updates itself once a day (YouTube changes things often).


ABOUT RIGHTS
------------
The loops are taken from other people's recordings. For practice, beatmaking and live sets
at home this is normal digging practice. If you release a track with a sample, it has to be
cleared with the rights holder; credits.txt keeps everything you'll need for that.
Downloading from YouTube may violate its terms — the decision is yours.
