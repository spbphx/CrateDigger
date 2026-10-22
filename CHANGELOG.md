# Changelog

## 1.3.3
- Scissors ✂ button on "drums only" loops: cuts the loop into one-shot samples and downloads them all as one ZIP. The button sits in the loop's action row, next to favorite / save / remove. All action buttons are the same size (34 px); the four buttons of a "drums only" loop stand with no gap, and the three buttons of other loops are spaced wider so both rows have the same total width.
- The one-shot ZIP is named `1shots_<key>_<date>_<time>.zip` (e.g. `1shots_Amin_2026-10-22_05-07.zip`).
- One-shot detection was rebuilt for dense funk breaks: onsets are pooled from three spectral bands plus the full-band envelope (a new hit is often masked in the full envelope by the tail of the previous one, but always rises in its own band — a hat high, a kick low), each candidate must show a real energy step big enough for that band, and every segment is rescanned so hits that were still masked split it. The result is a true one-shot per hit: a loop that used to give 24 samples with several hits inside now gives 32 clean ones (16ths of both bars).

## 1.3.2
- "Download jam" button in the jam bar: a ZIP of the loops playing now, each pitched exactly as it plays in the jam (speed = master BPM / loop BPM), named after the master loop (`Gmaj95-<date>-<time>.zip`). The key and BPM at the start of each loop name change to the values after the pitch shift.

## 1.3.1
- Key highlighting during a jam follows the master loop (the first one started) and takes the pitch shift into account: every next loop is played at the master's tempo, so e.g. Fmin 75 and F#min 80 are outlined under Emin 70. Loops more than 35 cents out of tune after the shift are not outlined. With "Tempo from first loop" off, original keys are compared; with nothing playing, hovering works as before.
- The master loop's key is shown next to the BPM in the jam bar.

## 1.3

**Better "Only" detection**
- Vocals and pads are never taken from parts with a beat: a repeating kick is caught even at slow tempos (kick on beats 1 and 3), where the general kick limit used to let it through.
- Pads need a tone: crowd noise, applause, talk and wind without harmony are no longer "pads only".
- Pads with a melody on top come later in the queue than static ones.

**Order of loops with several "Only" types**
- The types take turns (for example pads, drums, pads, drums), starting with the type that has the cleanest part; inside a type clean parts come before "with extras". A type repeats in a row only when the others have nothing left. "Loops per record" stays the total for the record.
- "Only" now also works with 4-bar loops.

**Seams of pads and vocals**
- If a chord or a sung note starts slightly before the beat (soft attack, reverb), both loop boundaries move to the real start of the attack, so the attack no longer clicks at the end of the loop. The old tail fade, which itself caused a dip before the restart, is gone for these loops.
- Vocal loops whose loudness at the start and at the end differ a lot, and off-grid pads where something new comes in during the last half second, are used only when nothing else was found.

**Other**
- Years and BPM filters are off by default on the first start.
- Debugging mode (More settings): purity of every loop and a 👎 wrong match button that saves the loop and the analyzer's readings to a Feedback folder.
- All code comments and program messages are written in English (Russian and Spanish are translations); functions got explanatory docstrings. Behaviour of the unchanged parts was verified bit-for-bit against 1.2.2.

## 1.2.2
- Interface in English, Russian and Spanish (flags in the header).
- Floppy-disk button to save a loop's WAV when drag-and-drop doesn't work.
- "Stop" interrupts searching and downloading immediately.

## 1.2.1
- Seam check: loops no longer click at the end when the next hit starts slightly early.
- Dark filtered breaks (bass + kick + snare) are no longer taken as "bass only".

## 1.2
- Pipeline: downloads and analysis run in parallel (network strictly one request at a time).
- Lower memory use; stereo 24-bit only by default.

## 1.1
- "Only" bass / vocals / pads (several at once), type letter in file names.

## 1.0
- First version: search, download, beat-exact loops fitted to multiples of 5 BPM, key detection, favourites, re-run, ZIP for the Model:Samples.
