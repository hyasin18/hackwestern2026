# CoCube voice system — setup and testing

The cube now talks, **through its own speaker only**:

| Cube state | What it does | Where the sound comes from |
|---|---|---|
| 🟢 Calm | Silent | — |
| 🟡 Getting louder | Says a short calming suggestion **once** | Gemini writes it → ElevenLabs speaks it → `generated_audio/` → cube speaker. No internet: `audio/default_yellow.wav` |
| 🔴 Too loud | Plays the parent's recording **once** | `audio/parent_voice.wav` → cube speaker. None uploaded: `audio/default_red.wav` |
| 🔵 Button short-press | 1-minute breathing with "Breathe in / Hold / Breathe out" in step with the light | Pre-recorded files in `audio/` → cube speaker |
| 🟣 Button held 2 s | "I need a break" on the dashboard (unchanged) | — |

The phone/dashboard **never** plays the cube's messages. It is only used to
upload the parent's recording (its "Listen" button is just a preview for the parent).

---

## 1. How it fits together

```
 USB mic ──► calm_cube.py (main loop, 50x a second, never waits)
               │  loudness → stress → zone, LEDs, button, dashboard, CSV log
               │
               │  every frame: VOICE.update(stress, turned_red, mode)
               ▼
            voice.py ── decides WHAT to say and WHEN (transitions, cooldowns,
               │         priorities, fallbacks). Slow work runs in threads.
               │
     yellow ───┼──► request_tip() ──► gemini_tips.py (unchanged)
               │         text ──► elevenlabs_voice.py ──► generated_audio/tts_….wav
     red ──────┼──► voice_messages.parent_voice_path() (unchanged) or default_red.wav
     breathing ┼──► audio/breathe_in.wav, hold.wav, breathe_out.wav
               ▼
         audio_manager.py ── ONE background thread, ONE sound at a time,
               │              priority: breathing 30 > red 20 > yellow 10
               ▼
         Player: "mmrplay -a alsa:default <file>"  (QNX)   or  SIMULATION
               ▼
         USB audio device ──► amplifier ──► speaker inside the cube

 Phone ──► POST /voice ──► parent_audio_upload.py (checks it) ──► audio/parent_voice.wav
 Phone ──► GET /voice_upload ──► voice_upload.html (converts any recording to WAV in the browser)
```

---

## 2. What I found in your existing code

**Files inspected:** `calm_cube.py`, `dashboard.html`, `gemini_tips.py`,
`reports.py`, `voice_messages.py`, `README.md`.

* **Zones** (`calm_cube.py`): a 0–100 *stress* value. `calm` < 35 ≤ `rising` < 65 ≤
  `high` < 85 ≤ `red`. "Turned red" fires when stress crosses **85** and re-arms
  below **60**. Colours come from each mode's palette (kid: teal-green → yellow → orange → magenta).
  So "yellow" = the *Getting louder* zone; I use stress ≥ **40** (re-arm below 25).
* **Gemini** (`gemini_tips.py`): `get_tip(ctx)` returns `(text, source)`; it only
  ever sends numbers, never sound. `calm_cube.request_tip()` called it on red and on
  break requests, in a background thread.
* **Button**: polled every 20 ms with 30 ms debounce. `"short"` on release under
  2 s, `"long"` once at 2 s while held — they can't be confused. Short press
  toggles breathing (4 s in, 2 s hold, 6 s out, ×5 = 1 minute).
* **Dashboard / HTTP**: one `ThreadingHTTPServer` on port 8080, routes
  `/status`, `/mode`, `/breathe`, `/ack`, `/tip`, `/reset`, plus `voice_messages.py`
  (`POST /voice?zone=red`, `GET /voice/red`, `GET /voice_delete`) and `reports.py`.
* **The dashboard already has an upload control** — but only in **Gentle (kid)
  mode**, under "What the cube says → Too loud". It accepts any `audio/*`.
* **Audio assumptions**: recording uses `arecord` (piped, or `--chunk` mode).
  There was **no playback code at all** before this.
* **QNX problems found:**
  1. The Pi 5 has **no headphone jack**. QNX's quick start image (QSTI) sends audio to
     **USB audio** on the Pi 5 by default, and documents `mmrplay` (WAV/AAC) for playback.
  2. Phones record **m4a**, which the cube can't be assumed to play. voice_messages.py
     would save it, and the cube would fail at red time. Handled — see section 6.
  3. The QSTI has Python 3 but **no pip**, so everything here is standard library only.
  4. The cube's own voice would be picked up by its mic and raise the stress meter.
     Handled: the mic is ignored while the cube talks (`MUTE_MIC_WHILE_SPEAKING`).
* **Gamer mode**: the README mentions it, but `calm_cube.py` and the dashboard only have
  **kid** and **focus**. I did not invent it — tell me if a teammate has that code.

---

## 3. Files

### New files
| File | What it does |
|---|---|
| `voice.py` | Decides what to say and when: yellow/red transitions, cooldowns, priorities, fallbacks, breathing cues, `/voice_status` and `/voice_test` pages |
| `elevenlabs_voice.py` | ElevenLabs API (stdlib `urllib`), caching, error handling |
| `audio_manager.py` | One-at-a-time playback thread, priorities, interruption, player abstraction (QNX command / laptop / simulation), WAV checking & conversion |
| `parent_audio_upload.py` | Strict checks for parent uploads (in front of voice_messages.py), serves `/voice_upload` |
| `voice_upload.html` | Separate upload page: converts any phone recording to WAV in the browser |
| `voice_config.py` | Reads settings from environment / `config.local` / defaults (4 modules need them, so they live in one place) |
| `make_default_audio.py` | Makes/checks the pre-recorded files (ElevenLabs or espeak) |
| `test_voice.py` | All the tests below |
| `audio/*.wav` | 8 pre-recorded sounds (see section 5) |
| `generated_audio/.gitkeep` | Folder for ElevenLabs audio (contents not committed) |
| `config.example`, `.gitignore`, `requirements.txt`, this file | Setup |

**Changed from your suggested structure:** files stay **flat in the repo root**
(no `calm_cube/` subfolder), because `calm_cube.py` loads `dashboard.html` from its own
folder and `voice_messages.py` saves into `./audio/` — moving them would break both.
`calm_down.wav` became `breathing_done.wav` ("Well done"), because `gemini_tips.py`'s own
rules say never to tell the child "calm down". Added `hold.wav` (the exercise has a 2 s hold).

### Existing files modified
Only **`calm_cube.py`**. Every change is marked and nothing was removed:
1. Imports `voice` / `parent_audio_upload` (if missing, the cube runs silently as before).
2. `request_tip(why, on_done=None)` — can hand the tip to voice.py; never sends two Gemini requests at once.
3. `do_POST`/`do_GET` route uploads through `parent_audio_upload` first, plus `/voice_upload`, `/voice_status`, `/voice_test`.
4. `breathe_phase()` — same timing as `breathe_brightness()`, so cues match the light.
5. Main loop: ignores the mic while speaking; calls the voice system each frame; breathing start/stop/finish hooks; kid-mode auto-breathing waits until the parent's message ends; voice events added to the CSV log.
6. `--sim` keys `1` (yellow), `2` (red), `0` (calm); new flags `--test-speaker`, `--audio-player`, `--no-voice`.

### Not modified (checked with SHA-256 before/after)
`dashboard.html`, `gemini_tips.py`, `reports.py`, `voice_messages.py`.

### Folder structure
```
hackwestern2026/
├── calm_cube.py              (modified)
├── dashboard.html            (unchanged)
├── gemini_tips.py            (unchanged)
├── reports.py                (unchanged)
├── voice_messages.py         (unchanged)
├── README.md                 (unchanged)
├── voice.py                  new
├── elevenlabs_voice.py       new
├── audio_manager.py          new
├── parent_audio_upload.py    new
├── voice_upload.html         new
├── voice_config.py           new
├── make_default_audio.py     new
├── test_voice.py             new
├── config.example            new  → copy to config.local (private)
├── .gitignore                new
├── requirements.txt          new  (no packages needed)
├── README_VOICE_SETUP.md     new  (this file)
├── audio/
│   ├── breathe_in.wav  hold.wav  breathe_out.wav  breathing_done.wav
│   ├── default_yellow.wav  default_red.wav  speaker_test.wav  test_tone.wav
│   └── parent_voice.wav + parent_voice.json   ← created by the upload (not in Git)
└── generated_audio/          ← ElevenLabs files, made on the cube (not in Git)
```

---

## 4. Hardware: the speaker on a Raspberry Pi 5 running QNX

**What is verified from QNX documentation (not yet on your cube):**
* The QNX 8.0 quick start image for Pi 5 supports audio playback (WAV/AAC) and recording.
* On the Pi 5 the default output is a **USB audio device** (`alsa:default` or `alsa:USB`).
* Playback command: `mmrplay -a "alsa:default" <file>`.
* QNX's hardware list recommends a **USB headset** for audio on the Pi 5.

**What that means for the speaker inside the base:**
* A bare speaker **cannot** be wired to the Pi 5 — there is no audio jack and the GPIO
  pins can't drive a speaker. It needs a **USB audio device + an amplifier**:
  * **Easiest:** a small **USB-powered speaker** that shows up as a USB sound card
    (has its own amplifier). Plug it into a blue USB 3 port.
  * **Or:** a cheap **USB sound-card dongle** (3.5 mm out) → a small **5 V amplifier board**
    (e.g. PAM8403/PAM8302 class) → the speaker. A passive speaker straight on the dongle
    will be very quiet.
  * Avoid I2S DAC/amp boards (e.g. MAX98357A) and HDMI audio: QNX's Pi 5 docs don't list them.
* Many cheap USB "speakers" only use USB for **power** and take sound through a 3.5 mm plug —
  those won't work on a Pi 5. It must be a **USB audio** device.
* You already have a USB mic. With two USB audio devices, `alsa:default` may pick either —
  test, and try `alsa:USB`.

### Questions for Ola (please answer before the demo)
1. **Exact speaker hardware:** brand/model of the speaker, and is there a USB sound card or amplifier? How is it powered?
2. On the cube, what does `which mmrplay wave aplay arecord` print?
3. Does `mmrplay -a alsa:default audio/test_tone.wav` make a sound? (Section 8, Test 1.)
4. Is the USB mic its own device, or part of the same USB sound card?
5. Has `python3 calm_cube.py --test-mic` (with `arecord`) already worked on the cube, or do you use `--chunk` / `--audio-cmd`?
6. Does `mmrplay` exit by itself when a file finishes? (If not, the cube stops it automatically 5 s after the sound's length — fine, but the next sound would wait.)
7. Optional: does `mmrplay some_iphone_memo.m4a` play? If yes, add `m4a,aac` to `PARENT_UPLOAD_FORMATS`.

---

## 5. Setup, step by step

### Step 1 — Put the files in your project
Copy every file from this delivery into your repo folder (the one with `calm_cube.py`),
keeping the `audio/` and `generated_audio/` folders. Your four protected files are not touched.

### Step 2 — ElevenLabs key (kept private)
1. On elevenlabs.io: profile → **API Keys** → create a key.
2. In the project folder:
   ```bash
   cp config.example config.local
   nano config.local          # or any editor
   ```
   Set `ELEVENLABS_API_KEY=sk_...your key...`
3. Optional: in ElevenLabs open **Voices**, choose a calm, warm voice, copy its **Voice ID**
   into `ELEVENLABS_VOICE_ID=`.
4. Check it is ignored by Git: `git status` must **not** list `config.local`.

Or use an environment variable instead of the file: `export ELEVENLABS_API_KEY=sk_...`.
The key is never printed, logged, put in `/status`, `/voice_status`, the dashboard or HTML.

### Step 3 — (Optional, recommended) nicer pre-recorded voice
The `audio/*.wav` files shipped here were made with the free **espeak-ng** voice (robotic, but
they work). To make all of them once with your ElevenLabs voice (about 200 characters of credit):
```bash
python3 make_default_audio.py --elevenlabs
python3 make_default_audio.py --check
```
Or record your own: any WAV with the same file name works. `--check` warns if a breathing cue
is longer than its phase (Breathe in < 3.5 s, Hold < 1.7 s, Breathe out < 5.5 s).

### Step 4 — Copy to the cube
From your laptop (QSTI's user is `qnxuser`):
```bash
scp -r hackwestern2026 qnxuser@<cube-ip>:~/
ssh qnxuser@<cube-ip>
cd ~/hackwestern2026
```
If the cube needs internet for Gemini/ElevenLabs, it must be on Wi-Fi. QNX's certificates at
`/etc/ssl/certs/cacert.pem` are used automatically (same as gemini_tips.py).

### Step 5 — Run
```bash
python3 calm_cube.py                  # normal (kid mode)
python3 calm_cube.py --mode focus
python3 calm_cube.py --sim            # no mic/LEDs; keys: 1 yellow, 2 red, 0 calm, p, l, q
python3 calm_cube.py --sim --audio-player sim   # also no sound (laptop logic test)
python3 calm_cube.py --no-voice       # exactly the old silent behaviour
```
At start-up the cube says how it will play sound, e.g.:
```
[voice] speaker: QNX (AUDIO_PLAY_CMD)  [mmrplay -a alsa:default {file}]
[voice] ElevenLabs key: set
[voice] red message: parent_voice.wav
```
If it says `SIMULATION`, no sound will come out — see Troubleshooting.

---

## 6. Parent recording — how to upload

**Recommended: the separate upload page** (works with any phone recording):
1. Phone on the same Wi-Fi as the cube.
2. Open `http://<cube-ip>:8080/voice_upload`
3. **Choose recording** (e.g. an iPhone Voice Memo — "Hey sweetheart, I'm here. Let's take a little break.")
4. The page converts it to a WAV (mono, 22 kHz, silence trimmed, volume evened out), you preview it, tap **Save on the cube**.
5. Tap **Play it on the cube** — it plays from the **cube's speaker**.
6. Upload again any time to replace it; **Remove** goes back to the default message.

**The dashboard's own button** (Gentle mode → "What the cube says" → Upload recording):
works for **WAV files only** by default. Honest limitation: if a parent picks an m4a there, the
cube refuses it (it couldn't play it at red time), and because `dashboard.html` can't be changed,
the dashboard only shows its generic *"Upload didn't work…"* message. The refusal message (visible in
the cube's console) points to `/voice_upload`. If Ola confirms `mmrplay` plays m4a on the cube,
set `PARENT_UPLOAD_FORMATS=wav,m4a,aac` and the dashboard button accepts iPhone recordings directly.

**Safety checks on every upload:** local network only · optional PIN · ≤ 8 MB · real type read
from the file's bytes (programs/scripts refused) · WAV opened and checked, 0.3–60 s · the
phone's file name is ignored (always saved as `audio/parent_voice.wav`, so no path tricks) ·
saved non-executable · atomic write. Same files voice_messages.py uses, so the dashboard's
Listen/Replace/Remove keep working.

**Don't expose port 8080 to the internet** (no port forwarding). On shared hackathon Wi-Fi,
anyone on the network can reach the dashboard — set `UPLOAD_PIN=` in `config.local` and upload
with `/voice_upload` (it has a PIN box).

---

## 7. Behaviour details (so you can explain them to judges)

* **Transitions, not continuous:** yellow fires once on the way *up* past 40 and can't fire
  again until stress drops below 25 **and** 90 s have passed. Red reuses the existing
  "turned red" (85 up / 60 reset) plus a 120 s cooldown. Staying loud = one Gemini request,
  one ElevenLabs request, one red message.
* **No overlap:** one playback thread. Priority breathing 30 > red 20 > yellow 10.
  Higher stops lower immediately; lower waits briefly or is dropped.
* **Breathing:** short press stops any AI/red message, then cues play at each light phase.
  Yellow/red are not spoken during breathing. Auto-breathing (kid mode) waits for the parent's
  message to finish instead of cutting it off.
* **Stale messages dropped:** a yellow message still being prepared is thrown away if the cube
  turns red, breathing starts, or the room calms down.
* **Duplicates avoided:** identical text + voice is generated once and reused from `generated_audio/` (max 60 files kept).
* **Errors:** invalid key / no credit / unknown voice → ElevenLabs paused 10 min; network or
  timeout → paused 30 s; every failure → fallback file. A missing/damaged sound file is skipped
  with a message, never a crash. A player that hangs is stopped automatically.
* **Never blocks:** the main loop only compares numbers; Gemini, ElevenLabs and playback run in threads.

---

## 8. Testing checklist

Run these **on the cube** in the project folder. Automatic tests also run on a laptop.

### Test 1 — Audio hardware
```bash
mmrplay -a alsa:default audio/test_tone.wav      # QNX's own tool, before any Python
python3 test_voice.py speaker                    # same as: python3 calm_cube.py --test-speaker
```
Expected:
```
Player: QNX (AUDIO_PLAY_CMD)
Command: mmrplay -a alsa:default {file}
Playing test_tone.wav ...
  player result: played
Playing speaker_test.wav ...
  player result: played
Software side: the player command finished without errors
>>> Did you hear a chime AND a voice from the speaker INSIDE the cube? [y/n] y
  PASS  player command ran without errors
  PASS  a person heard the sound from the cube's speaker
```
"player result: played" only means the OS accepted the audio. **Only your "y" confirms the
speaker works**; the answer is saved and shown in `/voice_status` as `speaker_confirmed_by_person`.
If silent: try `mmrplay -a alsa:USB audio/test_tone.wav`, then set that in `config.local`.

### Test 2 — Breathing voice
```bash
python3 test_voice.py breathing          # one 12 s cycle, prints a light bar + timing
```
Expected (times vary a little):
```
  light phase starts       cue started      difference
  in     at   0.00 s      breathe_in.wav   +0.00 s
  hold   at   4.01 s      hold.wav         -0.16 s
  out    at   6.01 s      breathe_out.wav  -0.14 s
  PASS  cues start within 0.35 s of the light (worst 0.16 s)
```
Then the real thing: `python3 calm_cube.py`, wait for calibration, **short-press** the button.
Watch the LEDs brighten while it says "Breathe in", stay bright on "Hold", fade on "Breathe out",
5 times, then "Well done". If the voice sounds late, raise `BREATHING_CUE_LEAD_S` (e.g. `0.4`).
Short-press again mid-exercise: lights and voice stop.

### Test 3 — Yellow / ElevenLabs
```bash
python3 elevenlabs_voice.py "It's getting a little loud. Let's take a slow breath together."
python3 test_voice.py yellow
```
Expected:
```
1. Gemini (1.2 s, source: gemini):
   (your suggestion text - it differs every time)
  PASS  Gemini wrote the suggestion (source=gemini)
2. ElevenLabs: generated in 0.9 s
  PASS  audio generation worked
  PASS  audio file saved: …/generated_audio/tts_….wav (2.8 s)
3. Playing through: QNX (AUDIO_PLAY_CMD)
  PASS  player command finished OK (played )
>>> Did you hear the suggestion from the CUBE's speaker? [y/n]
```
Each step is reported separately: **generation**, **saved file**, **speaker**.
In the full app: `python3 calm_cube.py --sim`, press `1` + Enter → console shows
`[voice] yellow: asking Gemini…` then `[audio] playing: yellow: ElevenLabs speech`.

### Test 4 — Red / parent recording
1. Upload at `http://<cube-ip>:8080/voice_upload`, tap **Play it on the cube**.
2. `python3 test_voice.py red` — expected:
   ```
   PASS  a parent recording has been uploaded
   PASS  the uploaded recording is playable
   Now playing: test red: parent recording
   PASS  the cube chose the parent's recording (not the default)
   ```
3. In the app: `python3 calm_cube.py --sim`, press `2` → `[audio] playing: red: parent recording (parent_voice.wav)`.

### Test 5 — Fallback
Automatic (pretends the internet is down, plays the fallbacks through the speaker):
```bash
python3 test_voice.py fallback --audio-player auto
```
Expected:
```
[voice] yellow: ElevenLabs failed [network] could not reach ElevenLabs: … -> fallback recording
  PASS  yellow used default_yellow.wav when offline
  PASS  red used default_red.wav when there is no parent recording
  PASS  a damaged parent recording falls back to default_red.wav (no crash)
```
Real test: turn off the Wi-Fi / unplug the network, run `python3 calm_cube.py --sim`, press `1` →
you hear `default_yellow.wav`, nothing crashes. (Note: a tip spoken before while online may be
reused from `generated_audio/`, which is correct offline behaviour.)

### Test 6 — Cooldown
```bash
python3 test_voice.py cooldown        # simulates 7 minutes in a few seconds, silent
```
Expected: `8 of 8 checks passed.` including
`staying loud for 3 minutes: Gemini asked once` and `...and the red message played once`.
Real test: `python3 calm_cube.py`, make continuous noise for 3 minutes — the cube speaks once
for yellow, once for red, then stays quiet (console: `red: staying quiet (cooldown, 97 s left)`).

### Test 7 — Priority
```bash
python3 test_voice.py priority        # add --audio-player auto to hear it
```
Expected: `8 of 8 checks passed.` including `button breathing stops the AI message` and
`'Breathe in' starts 0.01 s after the button`.
Real test: `--sim`, press `1` (yellow starts talking), then `p` → it stops mid-sentence and says "Breathe in".

### Test 8 — Existing functionality
```bash
python3 test_voice.py existing        # starts calm_cube.py --sim on port 18080, silent
python3 test_voice.py all             # every automatic test (about 1 minute)
```
Expected: `17 of 17 checks passed.` (dashboard loads, /status fields, short press/long press,
"I'm coming", red count, mode switch, CSV report and log, no Python errors).
Then on the cube by hand: `--test-mic`, `--test-leds`, normal run, dashboard on a phone,
Gentle/Focus switch, break request, "Show another" tip, CSV downloads.

Handy while testing: `http://<cube-ip>:8080/voice_status` shows the player, last results,
cooldowns, the parent recording, problems with any sound file, and recent voice events.
`http://<cube-ip>:8080/voice_test?play=tone` (or `breathe_in`, `red`, `yellow`, …) plays one thing.

---

## 9. Still needs physical testing on the QNX cube

| Item | Status |
|---|---|
| Logic: transitions, cooldowns, priorities, fallbacks, upload checks, existing features | Tested in simulation (all automatic tests pass) |
| ElevenLabs request/response/error handling | Tested against a fake server; **not yet with your real key** |
| `mmrplay` present and playing on your image | **Not tested** (documented by QNX only) |
| Sound actually coming out of the speaker in the base | **Not tested** — needs Test 1 "y" |
| USB speaker + USB mic working at the same time | **Not tested** |
| `mmrplay` start-up delay (breathing sync) | **Not tested** — Test 2 by eye/ear |
| Volume loud enough through the enclosure | **Not tested** |
| `mmrplay` exits by itself when done | **Not tested** (auto-stop covers it) |
| m4a/AAC playback via mmrplay | **Not tested** — WAV only until confirmed |
| Upload page on iPhone Safari / Android Chrome | **Not tested** on real phones |
| Mic muting while speaking stops self-triggering | Logic tested; acoustics **not tested** |

---

## 10. Privacy — exactly what is processed and kept

* **Microphone:** `arecord` streams raw audio into a pipe; Python reads 20 ms at a time, computes
  loudness (RMS/peak dB) and throws the samples away. Small buffers in the OS pipe and the audio
  driver briefly hold a fraction of a second of sound in memory; nothing is written to disk.
  **Exception — `--chunk` mode:** each second, 1 s of real room audio is written to
  `/tmp/calm_cube_chunk.wav`, read, and replaced by the next second; the last one stays there until
  the next run or reboot. The dashboard's privacy text ("no speech or sound from the room is
  stored") is accurate for the normal mode, not strictly for `--chunk` mode — worth telling Eliza.
* **While the cube talks** the mic frames are read and discarded unused (not stored either).
* **CSV log** (`calm_cube_log.csv`): loudness numbers, stress, mode, events (now including
  `say_yellow`, `say_red`, …). No audio.
* **Gemini:** numbers only (unchanged). **ElevenLabs:** only the suggestion's text — never
  microphone audio. ElevenLabs may keep generated items in your account history under its own
  policy. Generated speech is cached on the cube in `generated_audio/`.
* **Parent recording:** stored only on the cube in `audio/`; deleted with **Remove**.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Start-up says `SIMULATION … no player command found` | `which mmrplay`. If missing, set `AUDIO_PLAY_CMD` to a player that exists (`wave {file}`), `AUDIO_PLAYER=command` |
| `player result: failed 'mmrplay' exited with code …` | Read the message; try `-a alsa:USB`; check the USB audio device is plugged in **before** boot |
| Player OK but silent | Amplifier power/volume, wiring, USB "speaker" that is power-only, mic chosen as output — see section 4 |
| Some files play, others don't | Set `PLAYBACK_SAMPLE_RATE=48000` (and maybe `PLAYBACK_CHANNELS=2`) |
| Yellow always uses the fallback | `/voice_status` → `elevenlabs.last_error` (bad key, no credit, voice ID, no internet) |
| Dashboard upload says "didn't work" | It was probably m4a/mp3 — use `/voice_upload` |
| Cube's own voice raises the stress | Keep `MUTE_MIC_WHILE_SPEAKING=true`; raise `MUTE_TAIL_S` to `0.8` |
| Breathing voice late | Raise `BREATHING_CUE_LEAD_S` |
| Want it silent in Focus mode | `VOICE_IN_MODES=kid` |
