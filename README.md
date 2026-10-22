# CrateDigger

**Sample digging for boom bap.** CrateDigger searches YouTube Music for old records (1960–1999 by default, by your tags or at random), downloads them, finds the steadiest parts and cuts them into tight, seamless loops — ready for a DAW or an Elektron Model:Samples.

Full manual: [English](README_EN.txt) · [Русский](README.txt) · [Español](README_ES.txt)

## What it does

- Cuts loops exactly on the downbeats, 1, 2 or 4 bars, with a click check at the seam
- Detects key and tempo, fits the tempo to a multiple of 5 BPM (60–120) by time-stretching or by pitching (like a turntable pitch fader)
- Names files by key and tempo: `Gmaj110Quark1.wav`, `Amin95BQuark1.wav`
- Loop types: any, breaks, melody, or isolated parts — drums only, bass only, vocals only, pads only (several types at once take turns, the cleanest first)
- Plays several loops together in the window, in sync, so you can hear what fits; highlights compatible keys (Camelot)
- Favorites, re-running a record with other settings, drag-and-drop of loops into a folder, ZIP export sorted for Model:Samples
- Works with your own files too (vinyl rips): drop mp3 / wav / flac into the window
- Interface in English, Russian and Spanish
- Debugging mode: purity of every loop and a "wrong match" button that collects the loop and the analyzer's readings for tuning the detectors

## Demonstation

https://github.com/user-attachments/assets/e0e730c3-ff4b-486e-83c7-ce8dab7fef3e

## Quick start

You don't need to install anything by hand — the start file sets up Python and the libraries inside the program folder on the first launch (1–3 minutes, internet required).

1. Download the project: the green **Code** button → **Download ZIP**, and unpack it into a normal folder.
2. Launch:
   - **Windows:** double-click `START_WINDOWS.bat`
   - **macOS:** right-click `START_MAC.command` → Open
   - **Linux:** `bash START_LINUX.sh`
3. The program opens in your browser at `http://127.0.0.1:8765`. Keep the black console window open while digging.

Requirements: Windows 10/11, macOS or Linux; Python 3.10+ (installed automatically on Windows). The loops are saved to `Music/CrateDigger`.

## How it's built

A small local web app: a Python server (`app.py`) and a single-page interface (`ui.html`). Analysis and cutting live in `digger/` (numpy/scipy DSP, ffmpeg via imageio-ffmpeg), search and download use yt-dlp and ytmusicapi. Downloaded tracks are analysed in parallel processes while the next one downloads; requests to YouTube go one at a time with random pauses.

## Rights

The loops come from other people's recordings. Digging for practice, beatmaking and home jams is common practice, but if you release a track with a sample it has to be cleared with the rights holder — `credits.txt` in every crate keeps the artist, link and time of each loop. Downloading from YouTube may violate its terms of service; the decision is yours.

## License

[MIT](LICENSE) — for the program code. It does not cover the recordings you dig with it.
