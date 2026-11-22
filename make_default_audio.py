"""
Makes (or checks) the cube's pre-recorded sound files in audio/.

These standard messages are played from files - the cube never calls
ElevenLabs for them during use:

  breathe_in.wav      "Breathe in."            (must fit in the 4 s inhale)
  hold.wav            "Hold."                  (must fit in the 2 s hold)
  breathe_out.wav     "Breathe out."           (must fit in the 6 s exhale)
  breathing_done.wav  "Well done. You did it." (end of the exercise)
  default_yellow.wav  yellow fallback (no internet / no ElevenLabs)
  default_red.wav     red fallback (no parent recording yet)
  speaker_test.wav    used by the speaker test
  test_tone.wav       a soft two-note chime (no voice), made here in Python

The files that come with the project were made with the free espeak-ng
voice (robotic, but they work). For a nicer, matching voice, make them
ONCE with ElevenLabs on any computer with internet:

    python3 make_default_audio.py --elevenlabs      # uses config.local key
    python3 make_default_audio.py --espeak          # Linux with espeak-ng
    python3 make_default_audio.py --check           # are they all OK?

Or record them yourself: any WAV file with the same name works (keep the
breathing ones short - see the limits printed by --check).
"""
import argparse
import array
import math
import os
import shutil
import subprocess
import sys
import tempfile

import audio_manager as am
import voice_config

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(HERE, "audio")
RATE = 22050

PROMPTS = {
    "breathe_in": "Breathe in.",
    "hold": "Hold.",
    "breathe_out": "Breathe out.",
    "breathing_done": "Well done. You did it.",
    "default_yellow": "It's getting a little loud. Let's take a slow breath together.",
    "default_red": "It's okay. Let's take a little break together.",
    "speaker_test": "Hello! This is the Calm Cube speaker test.",
}

# The breathing timing from calm_cube.py (kept in sync automatically if possible)
try:
    from calm_cube import BREATHE_IN_S, BREATHE_HOLD_S, BREATHE_OUT_S
except Exception:
    BREATHE_IN_S, BREATHE_HOLD_S, BREATHE_OUT_S = 4.0, 2.0, 6.0
LIMITS = {"breathe_in": BREATHE_IN_S - 0.5, "hold": BREATHE_HOLD_S - 0.3,
          "breathe_out": BREATHE_OUT_S - 0.5, "breathing_done": 6.0,
          "default_yellow": 10.0, "default_red": 12.0, "speaker_test": 6.0,
          "test_tone": 2.0}


def path_of(name):
    return os.path.join(AUDIO_DIR, name + ".wav")


def make_tone():
    """Two soft notes (C5 then E5), gentle fades, about -12 dB."""
    out = array.array("h")
    for freq in (523.25, 659.25):
        n = int(0.35 * RATE)
        for i in range(n):
            env = min(1.0, i / (0.03 * RATE), (n - i) / (0.12 * RATE))
            out.append(int(8000 * env * math.sin(2 * math.pi * freq * i / RATE)))
        out.extend([0] * int(0.05 * RATE))
    if sys.byteorder == "big":
        out.byteswap()
    am.write_wav_from_pcm16(out.tobytes(), path_of("test_tone"), RATE)
    print("made  test_tone.wav")


def make_espeak(name, text):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise SystemExit("espeak-ng is not installed (Linux: sudo apt install espeak-ng)")
    tmp = os.path.join(tempfile.gettempdir(), "cocube_%s.wav" % name)
    voice = "mb-us1" if os.path.exists("/usr/share/mbrola/us1") else "en-us"
    subprocess.run([exe, "-v", voice, "-s", "135", "-w", tmp, text], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    am.convert_wav(tmp, path_of(name), rate=RATE, channels=1)
    os.remove(tmp)
    print("made  %s.wav  (espeak-ng, voice %s)" % (name, voice))


def make_elevenlabs(name, text, cfg):
    import elevenlabs_voice
    src, _ = elevenlabs_voice.synthesize(text, cfg)
    am.convert_wav(src, path_of(name), rate=RATE, channels=1)
    print("made  %s.wav  (ElevenLabs)" % name)


def check():
    ok = True
    for name in list(PROMPTS) + ["test_tone"]:
        try:
            info = am.wav_info(path_of(name))
            note = ""
            if info["seconds"] > LIMITS[name]:
                note = "  <-- TOO LONG (keep under %.1f s)" % LIMITS[name]
                ok = False
            print("ok    %-20s %4.1f s  %5d Hz  %2d-bit%s" %
                  (name + ".wav", info["seconds"], info["rate"], info["bits"], note))
        except am.BadAudio as e:
            print("BAD   %-20s %s" % (name + ".wav", e))
            ok = False
    print("\nAll sound files OK." if ok else "\nSome files need fixing (see above).")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--elevenlabs", action="store_true", help="make voices with ElevenLabs")
    g.add_argument("--espeak", action="store_true", help="make voices with espeak-ng")
    g.add_argument("--check", action="store_true", help="only check the files")
    ap.add_argument("--only", help="make just one, e.g. --only default_red")
    args = ap.parse_args()
    os.makedirs(AUDIO_DIR, exist_ok=True)
    if args.check:
        sys.exit(0 if check() else 1)
    cfg = voice_config.load()
    names = [args.only] if args.only else list(PROMPTS)
    for name in names:
        if name == "test_tone":
            continue
        if name not in PROMPTS:
            raise SystemExit("unknown name %s (choose from %s)" % (name, ", ".join(PROMPTS)))
        if args.elevenlabs:
            make_elevenlabs(name, PROMPTS[name], cfg)
        else:
            make_espeak(name, PROMPTS[name])
    if not args.only or args.only == "test_tone":
        make_tone()
    print()
    check()


if __name__ == "__main__":
    main()
