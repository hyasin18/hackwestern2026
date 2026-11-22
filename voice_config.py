"""
Settings for the CoCube voice system, in one place.

Where a setting comes from (first one found wins):
  1. an environment variable with the same name   (export ELEVENLABS_API_KEY=...)
  2. config.local  - your private copy of config.example (never committed to Git)
  3. the default written below

Standard library only, so it runs on the QNX quick start image as-is.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.local")

DEFAULTS = {
    # --- ElevenLabs (YELLOW messages) ---
    "ELEVENLABS_API_KEY": "",
    "ELEVENLABS_VOICE_ID": "JBFqnCBsd6RMkjVDRZzb",   # pick your own in ElevenLabs
    "ELEVENLABS_MODEL_ID": "eleven_flash_v2_5",      # low-latency model
    "ELEVENLABS_SAMPLE_RATE": "22050",               # asks for pcm_22050
    "ELEVENLABS_SPEED": "0.9",                       # 0.7 - 1.2; slower = calmer
    "ELEVENLABS_TIMEOUT_S": "10",

    # --- Speaker / playback ---
    "AUDIO_PLAYER": "auto",          # auto | command | sim | laptop
    "LAPTOP_SPEAKER_IP": "",         # for AUDIO_PLAYER=laptop (laptop_speaker.py)
    "AUDIO_PLAY_CMD": "mmrplay -a alsa:default {file}",
    "PLAYBACK_SAMPLE_RATE": "",      # blank = play files as they are, e.g. 48000
    "PLAYBACK_CHANNELS": "",         # blank, 1 or 2
    "VOLUME_GAIN": "1.0",            # 1.0 = unchanged, 0.5 = half, 2.0 = double
    "MUTE_MIC_WHILE_SPEAKING": "true",
    "MUTE_TAIL_S": "0.4",

    # --- When the cube talks ---
    "VOICE_ENABLED": "true",
    "VOICE_IN_MODES": "kid,focus",
    "YELLOW_AT": "40",               # stress that counts as "turned yellow"
    "YELLOW_RESET": "25",            # must fall below this before yellow can repeat
    "YELLOW_COOLDOWN_S": "90",
    "YELLOW_MAX_WAIT_S": "15",       # drop a yellow message that is this late
    "RED_COOLDOWN_S": "120",
    "BREATHING_VOICE": "true",
    "BREATHING_CUE_LEAD_S": "0.15",  # start cues a little early to hide start-up lag
    "AUTO_BREATHE_WAITS_FOR_VOICE": "true",

    # --- Parent recording upload ---
    "PARENT_UPLOAD_FORMATS": "wav",  # add m4a,aac ONLY after testing them on the Pi
    "PARENT_MAX_SECONDS": "60",
    "UPLOAD_LAN_ONLY": "true",
    "UPLOAD_PIN": "",                # optional; see README_VOICE_SETUP.md
}

SECRET_KEYS = ("ELEVENLABS_API_KEY", "UPLOAD_PIN")


def _read_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            values[k.strip().upper()] = v
    return values


def load():
    """Returns a Config with every setting filled in."""
    values = dict(DEFAULTS)
    values.update({k: v for k, v in _read_file(CONFIG_FILE).items() if k in DEFAULTS})
    for k in DEFAULTS:
        if os.environ.get(k, "").strip():
            values[k] = os.environ[k].strip()
    return Config(values)


class Config:
    def __init__(self, values):
        self.values = values

    def str(self, k):
        return self.values.get(k, DEFAULTS.get(k, ""))

    def float(self, k):
        try:
            return float(self.str(k))
        except ValueError:
            return float(DEFAULTS[k] or 0)

    def int(self, k):
        try:
            return int(float(self.str(k)))
        except ValueError:
            return 0

    def bool(self, k):
        return self.str(k).lower() in ("1", "true", "yes", "on")

    def list(self, k):
        return [x.strip().lower() for x in self.str(k).split(",") if x.strip()]

    def set(self, k, v):
        self.values[k] = str(v)

    def public(self):
        """All settings with secrets hidden (safe to show on a web page)."""
        out = {}
        for k, v in self.values.items():
            out[k] = ("set (hidden)" if v else "not set") if k in SECRET_KEYS else v
        return out
