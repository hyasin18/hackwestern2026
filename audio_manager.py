"""
Audio output for CoCube: plays sound files through the cube's speaker,
one at a time, without ever blocking the main loop.

  - One background thread does all playing. Nothing else waits for it.
  - Only one sound plays at a time. Each sound has a priority:
        breathing cue (30) > red message (20) > yellow message (10)
    A more important sound stops a less important one. A less important
    sound waits (a short while) or is dropped.
  - The actual "make a sound" step is a small Player object, so the rest
    of the project never depends on one hard-coded command.

Players
  CommandPlayer  runs a command such as  mmrplay -a alsa:default {file}
                 (QNX quick start image)  or  aplay {file}  (a Linux laptop).
  SimPlayer      plays nothing; prints what it would play and waits as long
                 as the sound lasts, so timing and overlap logic still run.

What "played" means here: the player command ran and exited without an
error. That proves the OS accepted the audio. It does NOT prove sound came
out of the speaker - only a person listening can confirm that
(python3 test_voice.py speaker records that answer).

Standard library only.
"""
import array
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "generated_audio")
SPEAKER_CHECK_FILE = os.path.join(CACHE_DIR, "speaker_check.json")

PRIORITY_YELLOW = 10
PRIORITY_RED = 20
PRIORITY_BREATHING = 30
PRIORITY_TEST = 40

MAX_UNKNOWN_LENGTH_S = 65      # watchdog for files whose length we can't read


class BadAudio(Exception):
    """A sound file is missing, damaged, or in a format we can't play."""


def is_qnx():
    try:
        return os.uname().sysname.upper().startswith("QNX")
    except AttributeError:
        return sys.platform.startswith("qnx")


# =============================================================================
# WAV checking and conversion (pure Python)
# =============================================================================

def wav_info(path, full_check=True):
    """Checks a WAV file and returns its details as a dict.
    Raises BadAudio with a plain-English reason if it can't be played."""
    if not os.path.isfile(path):
        raise BadAudio("file not found: %s" % os.path.basename(path))
    try:
        with wave.open(path, "rb") as w:
            ch, width = w.getnchannels(), w.getsampwidth()
            rate, n = w.getframerate(), w.getnframes()
            comp = w.getcomptype()
            got = len(w.readframes(n)) if full_check else n * ch * width
    except (wave.Error, EOFError, OSError, ValueError) as e:
        raise BadAudio("not a playable WAV file (%s)" % e)
    if comp != "NONE":
        raise BadAudio("compressed WAV files are not supported")
    if ch not in (1, 2):
        raise BadAudio("WAV must be mono or stereo, this has %d channels" % ch)
    if width not in (1, 2, 3, 4):
        raise BadAudio("unsupported WAV sample size (%d bytes)" % width)
    if not 8000 <= rate <= 48000:
        raise BadAudio("unsupported sample rate %d Hz" % rate)
    if n <= 0 or got == 0:
        raise BadAudio("the WAV file has no sound in it")
    if got < n * ch * width * 0.9:
        raise BadAudio("the WAV file is cut short (damaged)")
    return {"channels": ch, "bits": width * 8, "rate": rate, "frames": n,
            "seconds": n / float(rate)}


def _samples_16(raw, width):
    """Raw PCM bytes -> array of signed 16-bit ints."""
    if width == 2:
        a = array.array("h")
        a.frombytes(raw[:len(raw) - len(raw) % 2])
        if sys.byteorder == "big":
            a.byteswap()
        return a
    if width == 1:                                   # 8-bit WAV is unsigned
        return array.array("h", ((b - 128) << 8 for b in raw))
    if width == 3:
        return array.array("h", (int.from_bytes(raw[i + 1:i + 3], "little", signed=True)
                                 for i in range(0, len(raw) - 2, 3)))
    a = array.array("i")                             # 32-bit
    a.frombytes(raw[:len(raw) - len(raw) % 4])
    if sys.byteorder == "big":
        a.byteswap()
    return array.array("h", (s >> 16 for s in a))


def convert_wav(src, dst, rate=None, channels=None, gain=1.0):
    """Rewrites a WAV file as 16-bit PCM, optionally changing its sample rate,
    channel count and volume. Pure Python: fine for short messages."""
    with wave.open(src, "rb") as w:
        ch, width, src_rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        s = _samples_16(w.readframes(w.getnframes()), width)
    rate = rate or src_rate
    channels = channels or ch
    if ch == 2:                                       # work in mono
        s = array.array("h", ((s[i] + s[i + 1]) // 2 for i in range(0, len(s) - 1, 2)))
    if rate != src_rate and len(s) > 1:               # linear-interpolation resample
        n_out = int(len(s) * rate / float(src_rate))
        step = src_rate / float(rate)
        last = len(s) - 1
        out = array.array("h", bytes(2 * n_out))
        for j in range(n_out):
            pos = j * step
            i = int(pos)
            if i >= last:
                out[j] = s[last]
            else:
                f = pos - i
                out[j] = int(s[i] + (s[i + 1] - s[i]) * f)
        s = out
    if gain != 1.0:
        s = array.array("h", (max(-32768, min(32767, int(v * gain))) for v in s))
    if channels == 2:
        st = array.array("h", bytes(4 * len(s)))
        st[0::2] = s
        st[1::2] = s
        s = st
    if sys.byteorder == "big":
        s.byteswap()
    tmp = dst + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(s.tobytes())
    os.replace(tmp, dst)


def write_wav_from_pcm16(pcm, path, rate, channels=1):
    """Raw signed 16-bit little-endian PCM -> WAV file (written atomically)."""
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm[:len(pcm) - len(pcm) % 2])
    os.replace(tmp, path)


# =============================================================================
# Players
# =============================================================================

class SimPlayer:
    """Plays nothing. Prints what it would play and waits for its length."""

    def __init__(self, speed=1.0, reason="simulation mode"):
        self.speed = speed
        self.name = "sim"
        self.description = "SIMULATION (%s) - no sound comes out of any speaker" % reason
        self.is_real = False

    def play(self, path, seconds, stop_event, info):
        print("\n[sim audio] would play %s (%.1f s)" %
              (os.path.basename(path), seconds or 0))
        if stop_event.wait((seconds or 1.0) / self.speed):
            return "interrupted", ""
        return "simulated", "nothing was played (simulation)"


class CommandPlayer:
    """Plays a file by running a command. {file} is replaced by the file path;
    {rate} and {channels} by the WAV's values (handy for raw-PCM players).
    No shell is used, so file names can't inject commands."""

    def __init__(self, template, description):
        self.template = template
        self.args = shlex.split(template)
        if not any("{file}" in a for a in self.args):
            self.args.append("{file}")
        self.name = self.args[0] if self.args else "?"
        self.description = description
        self.is_real = True

    def available(self):
        return bool(self.args) and shutil.which(self.args[0]) is not None

    def play(self, path, seconds, stop_event, info):
        info = info or {}
        argv = [a.replace("{file}", path)
                 .replace("{rate}", str(info.get("rate", 44100)))
                 .replace("{channels}", str(info.get("channels", 1)))
                for a in self.args]
        with tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=err)
            except OSError as e:
                return "failed", "could not start '%s': %s" % (self.name, e)
            limit = time.time() + ((seconds + 5.0) if seconds else MAX_UNKNOWN_LENGTH_S)
            while proc.poll() is None:
                if stop_event.wait(0.03):
                    _stop_process(proc)
                    return "interrupted", ""
                if time.time() > limit:
                    _stop_process(proc)
                    return "failed", ("'%s' was still running long after the sound "
                                      "should have ended, so it was stopped" % self.name)
            err.seek(0)
            msg = err.read()[-400:].decode(errors="replace").strip()
        if proc.returncode != 0:
            return "failed", "'%s' exited with code %d. %s" % (self.name, proc.returncode, msg)
        return "played", msg


def _stop_process(proc):
    try:
        proc.terminate()
        proc.wait(timeout=0.5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def make_player(cfg, override=None):
    """Chooses how sound is played. Prints the choice so it's never a mystery."""
    kind = (override or cfg.str("AUDIO_PLAYER")).lower()
    if kind == "sim":
        return SimPlayer()
    if kind == "command":
        p = CommandPlayer(cfg.str("AUDIO_PLAY_CMD"), "your AUDIO_PLAY_CMD setting")
        if not p.available():
            print("WARNING: '%s' was not found on this system - sounds will fail." % p.name)
        return p
    # auto
    if is_qnx():
        options = [(cfg.str("AUDIO_PLAY_CMD"), "QNX (AUDIO_PLAY_CMD)"),
                   ("mmrplay -a alsa:default {file}", "QNX mmrplay"),
                   ("wave {file}", "QNX wave utility")]
    else:
        options = [("afplay {file}", "macOS laptop speaker - NOT the cube"),
                   ("aplay -q {file}", "Linux laptop speaker (ALSA) - NOT the cube"),
                   ("paplay {file}", "Linux laptop speaker (PulseAudio) - NOT the cube"),
                   ("ffplay -nodisp -autoexit -loglevel quiet {file}",
                    "laptop speaker (ffplay) - NOT the cube")]
    for template, desc in options:
        p = CommandPlayer(template, desc)
        if p.available():
            return p
    print("No audio player command found - falling back to SIMULATION (silent).")
    return SimPlayer(reason="no player command found")


def speaker_check():
    """What a person confirmed in 'python3 test_voice.py speaker', or None."""
    try:
        with open(SPEAKER_CHECK_FILE) as fh:
            return json.load(fh)
    except Exception:
        return None


# =============================================================================
# The audio manager
# =============================================================================

class Clip:
    def __init__(self, path, priority, label, max_wait_s, interrupt_same, on_done):
        self.path, self.priority, self.label = path, priority, label
        self.max_wait_s, self.interrupt_same = max_wait_s, interrupt_same
        self.on_done = on_done
        self.t_req = time.time()


class AudioManager:
    def __init__(self, player, cfg):
        self.player = player
        self.cfg = cfg
        self.gain = cfg.float("VOLUME_GAIN") or 1.0
        self.rate = cfg.int("PLAYBACK_SAMPLE_RATE") or None
        self.channels = cfg.int("PLAYBACK_CHANNELS") or None
        self.extra_formats = [f for f in cfg.list("PARENT_UPLOAD_FORMATS") if f != "wav"]
        self.mute_tail = cfg.float("MUTE_TAIL_S")
        self.cv = threading.Condition()
        self.current = None
        self.pending = None
        self.stop_event = threading.Event()
        self.last_end = 0.0
        self.history = deque(maxlen=30)
        os.makedirs(CACHE_DIR, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True, name="audio").start()

    # ---- checks ----------------------------------------------------------
    def check(self, path):
        """(True, info) if we can try to play this file, else (False, reason)."""
        if not path:
            return False, "no file"
        ext = os.path.splitext(path)[1][1:].lower()
        if ext == "wav":
            try:
                return True, wav_info(path)
            except BadAudio as e:
                return False, str(e)
        if ext in self.extra_formats:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return True, None                    # length unknown
            return False, "file not found or empty"
        return False, (".%s files are not enabled for playback (only WAV is "
                       "verified; see PARENT_UPLOAD_FORMATS)" % ext)

    def _prepare(self, path, info):
        """Converts a WAV to the format the speaker needs, if settings ask for one.
        Results are cached in generated_audio/."""
        if info is None:
            return path
        need = (info["bits"] != 16 or self.gain != 1.0 or
                (self.rate and info["rate"] != self.rate) or
                (self.channels and info["channels"] != self.channels))
        if not need:
            return path
        st = os.stat(path)
        key = hashlib.sha1(("%s|%s|%s|%s|%s|%s" % (path, st.st_mtime, st.st_size, self.rate,
                                                   self.channels, self.gain)).encode()).hexdigest()[:16]
        out = os.path.join(CACHE_DIR, "play_%s.wav" % key)
        if not os.path.exists(out):
            convert_wav(path, out, self.rate, self.channels, self.gain)
        return out

    # ---- public API --------------------------------------------------------
    def play(self, path, priority, label, max_wait_s=10.0, interrupt_same=False,
             on_done=None):
        """Asks for a sound. Returns True if it will play (now or soon),
        False if it can't be played or was dropped. Never blocks."""
        ok, info = self.check(path)
        if not ok:
            self._log(label, path, "skipped", info)
            return False
        clip = Clip(path, priority, label, max_wait_s, interrupt_same, on_done)
        with self.cv:
            cur = self.current
            if self.pending and self.pending.priority > priority:
                self._log(label, path, "dropped",
                          "'%s' is more important and already waiting" % self.pending.label)
                return False
            if cur and (priority > cur.priority or
                        (interrupt_same and priority == cur.priority)):
                self.stop_event.set()                 # stop what's playing now
            if self.pending:
                self._log(self.pending.label, self.pending.path, "dropped",
                          "replaced by '%s'" % label)
            self.pending = clip
            self.cv.notify()
        return True

    def stop(self, priority=None):
        """Stops the current/waiting sound (only ones of this priority, if given)."""
        with self.cv:
            if self.pending and priority in (None, self.pending.priority):
                self._log(self.pending.label, self.pending.path, "dropped", "stopped")
                self.pending = None
            if self.current and priority in (None, self.current.priority):
                self.stop_event.set()

    def is_busy(self):
        """True while a sound is playing (plus a short tail). The main loop uses
        this to ignore the microphone so the cube doesn't hear itself."""
        with self.cv:
            if self.current or self.pending:
                return True
        return time.time() - self.last_end < self.mute_tail

    def playing(self):
        """(label, priority) of the sound playing now, or None."""
        with self.cv:
            c = self.current
            return (c.label, c.priority) if c else None

    def status(self):
        p = self.playing()
        return {"player": self.player.name,
                "player_description": self.player.description,
                "player_is_real": self.player.is_real,
                "playing_now": p[0] if p else None,
                "speaker_confirmed_by_person": speaker_check(),
                "recent": list(self.history)[::-1][:12]}

    # ---- internals -----------------------------------------------------------
    def _log(self, label, path, result, detail=""):
        entry = {"t": round(time.time(), 1), "label": label,
                 "file": os.path.basename(path or ""), "result": result,
                 "detail": (detail or "")[:300]}
        self.history.append(entry)
        if result in ("played", "simulated") and not detail:
            text = "%s: %s" % (result, label)
        else:
            text = "%s: %s%s" % (result, label, (" - " + detail) if detail else "")
        print("\n[audio] " + text)

    def _worker(self):
        while True:
            with self.cv:
                while self.pending is None:
                    self.cv.wait()
                clip, self.pending = self.pending, None
                if time.time() - clip.t_req > clip.max_wait_s:
                    self._log(clip.label, clip.path, "dropped", "too late to be useful")
                    continue
                self.current = clip
                self.stop_event.clear()
            result, detail = "failed", ""
            try:
                ok, info = self.check(clip.path)          # it may have changed
                if not ok:
                    raise BadAudio(info)
                print("\n[audio] playing: %s (%s)" % (clip.label, os.path.basename(clip.path)))
                path = self._prepare(clip.path, info)
                seconds = info["seconds"] if info else None
                result, detail = self.player.play(path, seconds, self.stop_event, info)
            except BadAudio as e:
                result, detail = "skipped", str(e)
            except Exception as e:                        # never kill the thread
                result, detail = "failed", "%s: %s" % (type(e).__name__, e)
            with self.cv:
                self.current = None
                self.last_end = time.time()
            self._log(clip.label, clip.path, result, detail)
            if clip.on_done:
                try:
                    clip.on_done(result)
                except Exception as e:
                    print("\n[audio] on_done error: %s" % e)
