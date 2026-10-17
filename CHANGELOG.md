# Changelog

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
