"""
CoCube voice: decides WHAT the cube says and WHEN. All sound goes to the
cube's own speaker through audio_manager.py.

  GREEN  (calm)          silent.
  YELLOW (getting loud)  once, when stress climbs past YELLOW_AT:
                         Gemini writes a tip -> ElevenLabs speaks it.
                         No internet / no key -> audio/default_yellow.wav
  RED    (too loud)      once, when calm_cube.py says "turned red":
                         the parent's recording (audio/parent_voice.wav),
                         or audio/default_red.wav if there isn't one.
  BREATHING              "Breathe in" / "Hold" / "Breathe out" from
                         pre-recorded files, in step with the light.

Rules that stop the cube from nagging:
  - Yellow only fires on the way UP, and can't fire again until the room has
    been calm (stress below YELLOW_RESET). Plus a cooldown (YELLOW_COOLDOWN_S).
  - Red only fires when calm_cube.py's existing "turned red" fires (stress
    crosses 85, resets below 60). Plus a cooldown (RED_COOLDOWN_S).
  - So staying loud never triggers repeated Gemini/ElevenLabs requests.
  - Nothing yellow/red is said during a breathing exercise.
  - A yellow message that is still being prepared when the cube turns red
    (or breathing starts, or the room calms down) is thrown away.

calm_cube.py calls update() every frame. update() only compares numbers;
anything slow (Gemini, ElevenLabs, playing sound) happens in other threads.

Standard library only.
"""
import json
import os
import threading
import time
from collections import deque

import audio_manager as am
import elevenlabs_voice
import voice_config

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(HERE, "audio")

FILES = {                       # name -> file in audio/
    "breathe_in": "breathe_in.wav",
    "hold": "hold.wav",
    "breathe_out": "breathe_out.wav",
    "breathing_done": "breathing_done.wav",
    "yellow_fallback": "default_yellow.wav",
    "red_fallback": "default_red.wav",
    "tone": "test_tone.wav",
    "speaker_test": "speaker_test.wav",
}
PHASE_FILES = {"in": "breathe_in", "hold": "hold", "out": "breathe_out"}
TIP_WAIT_S = 12                 # longest we wait for Gemini before using the fallback


def audio_file(name):
    return os.path.join(AUDIO_DIR, FILES[name])


class VoiceSystem:
    def __init__(self, cfg=None, player_override=None, request_tip=None,
                 parent_path=None, tts=None, audio=None, quiet=False):
        """request_tip(why, on_done) asks Gemini for a tip (calm_cube.py's
        request_tip); on_done(tip, source) is called with the answer.
        parent_path() returns the parent's recording or None."""
        self.cfg = cfg or voice_config.load()
        c = self.cfg
        self.audio = audio or am.AudioManager(am.make_player(c, player_override), c)
        self.request_tip = request_tip
        self.parent_path = parent_path or (lambda: None)
        self.tts = tts or elevenlabs_voice.synthesize
        self.lock = threading.RLock()
        self.enabled = c.bool("VOICE_ENABLED")
        self.modes = c.list("VOICE_IN_MODES")
        self.yellow_at, self.yellow_reset = c.float("YELLOW_AT"), c.float("YELLOW_RESET")
        self.yellow_cd, self.red_cd = c.float("YELLOW_COOLDOWN_S"), c.float("RED_COOLDOWN_S")
        self.yellow_wait = c.float("YELLOW_MAX_WAIT_S")
        self.sound_delay = max(0.0, c.float("SOUND_DELAY_S"))
        self.yellow_armed = True
        self.last_yellow = self.last_red = -1e9
        self.red_requested = 0.0       # wall-clock time the last red message was asked for
        self.yellow_gen = 0            # bumping this cancels yellow messages in progress
        self.breathing = False
        self.last_phase = None
        self.stress = 0.0
        self.last_yellow_text = None
        self.events = deque(maxlen=40)
        self.csv_events = []
        if not quiet:
            self._startup_report()

    # =========================================================================
    # Called by calm_cube.py
    # =========================================================================
    def update(self, now, stress, turned_red, mode, calibrating):
        """Every frame. Cheap: just compares numbers."""
        with self.lock:
            self.stress = stress
            if calibrating or not self.enabled:
                return
            if stress < self.yellow_reset:
                self.yellow_armed = True               # room was calm: yellow may fire again
            if turned_red:
                self.yellow_armed = False              # no yellow tip while red
                self._red(now, mode)
            elif self.yellow_armed and stress >= self.yellow_at:
                self.yellow_armed = False
                self._yellow(now, mode)

    def breathing_started(self):
        with self.lock:
            self.breathing = True
            self.last_phase = None
            self.yellow_gen += 1                       # cancel yellow being prepared
        self.audio.stop()                              # stop any AI/red message cleanly

    def breathing_phase(self, phase):
        """phase = (cycle_number, "in" | "hold" | "out") from calm_cube.py.
        Plays a cue each time the phase changes, so cues follow the light."""
        with self.lock:
            if not self.breathing or phase is None or phase == self.last_phase:
                return
            self.last_phase = phase
        if self.cfg.bool("BREATHING_VOICE"):
            name = PHASE_FILES[phase[1]]
            self.audio.play(audio_file(name), am.PRIORITY_BREATHING,
                            "breathing: " + name.replace("_", " "),
                            max_wait_s=0.8, interrupt_same=True)

    def breathing_stopped(self, finished=False):
        with self.lock:
            was = self.breathing
            self.breathing = False
            self.last_phase = None
        self.audio.stop(am.PRIORITY_BREATHING)
        if was and finished and self.cfg.bool("BREATHING_VOICE"):
            self.audio.play(audio_file("breathing_done"), am.PRIORITY_BREATHING,
                            "breathing: done", max_wait_s=2)

    def mic_muted(self):
        """True while the cube is talking, so it doesn't hear its own voice."""
        return self.cfg.bool("MUTE_MIC_WHILE_SPEAKING") and self.audio.is_busy()

    def red_playing(self):
        """True while the red message plays (or is just about to start)."""
        p = self.audio.playing()
        if p and p[1] == am.PRIORITY_RED:
            return True
        return time.time() - self.red_requested < 1.0

    def playing_zone(self):
        """"red" / "yellow" while that message is waiting or playing, so the
        light can stay the same colour as the voice."""
        if self.red_playing():
            return "red"
        p = self.audio.playing()
        if p and p[1] == am.PRIORITY_YELLOW:
            return "yellow"
        return None

    def speaking(self):
        p = self.audio.playing()
        return p[0] if p else None

    def pop_csv_events(self):
        with self.lock:
            ev, self.csv_events = self.csv_events, []
        return ev

    # =========================================================================
    # Yellow and red
    # =========================================================================
    def _note(self, text):
        self.events.append({"t": round(time.time(), 1), "text": text})
        print("\n[voice] " + text)

    def _mode_ok(self, mode, what):
        if mode not in self.modes:
            self._note("%s: voice is off in %s mode (VOICE_IN_MODES)" % (what, mode))
            return False
        if self.breathing:
            self._note("%s: skipped, breathing exercise in progress" % what)
            return False
        return True

    def _red(self, now, mode):
        self.yellow_gen += 1                           # a late yellow message is now stale
        if not self._mode_ok(mode, "red"):
            return
        left = self.red_cd - (now - self.last_red)
        if left > 0:
            self._note("red: staying quiet (cooldown, %d s left)" % left)
            return
        self.last_red = now
        self.red_requested = time.time() + self.sound_delay
        # Checking the recording reads it from the SD card - do that off the main loop.
        threading.Thread(target=self._red_later, daemon=True).start()

    def _red_later(self):
        """The light turns red first; the message follows SOUND_DELAY_S later."""
        time.sleep(self.sound_delay)
        with self.lock:
            if self.breathing:
                self._note("red: skipped, breathing started before the message")
                return
        self._play_red("red")

    def _play_red(self, why):
        for label, path, csv in (("parent recording", self.parent_path(), "say_red"),
                                 ("default red message", audio_file("red_fallback"),
                                  "say_red_fallback")):
            if not path:
                continue
            ok, reason = self.audio.check(path)
            if ok:
                self.audio.play(path, am.PRIORITY_RED, "%s: %s" % (why, label), max_wait_s=5)
                with self.lock:
                    self.csv_events.append(csv)
                return True
            self._note("%s: %s can't be played (%s)" % (why, label, reason))
        self._note("%s: no playable red message at all" % why)
        return False

    def _yellow(self, now, mode, force=False):
        if not force:
            if not self._mode_ok(mode, "yellow"):
                return
            left = self.yellow_cd - (now - self.last_yellow)
            if left > 0:
                self._note("yellow: staying quiet (cooldown, %d s left)" % left)
                return
        self.last_yellow = now
        self.yellow_gen += 1
        gen = self.yellow_gen
        self._note("yellow: asking Gemini for a calming suggestion")
        threading.Thread(target=self._yellow_work, args=(gen, force, time.time()),
                         daemon=True).start()

    def _still_wanted(self, gen, force):
        with self.lock:
            if gen != self.yellow_gen or self.breathing:
                return False
            return force or self.stress >= self.yellow_reset

    def _yellow_work(self, gen, force, t0=None):
        text, src = self._get_tip()
        if not self._still_wanted(gen, force):
            self._note("yellow: message no longer needed, dropped")
            return
        path, how = None, ""
        if text:
            self.last_yellow_text = {"text": text, "gemini": src, "t": time.time()}
            try:
                path, cached = self.tts(text, self.cfg)
                how = "ElevenLabs speech" + (" (reused)" if cached else "")
            except elevenlabs_voice.TTSError as e:
                self._note("yellow: ElevenLabs failed [%s] %s -> fallback recording" % (e.kind, e))
        else:
            self._note("yellow: no suggestion from Gemini (%s) -> fallback recording" % src)
        if not path:
            path, how = audio_file("yellow_fallback"), "fallback recording"
        if t0 is not None and not force:              # light first, sound a bit later
            time.sleep(max(0.0, self.sound_delay - (time.time() - t0)))
        if not self._still_wanted(gen, force):
            self._note("yellow: message no longer needed, dropped")
            return
        if self.audio.play(path, am.PRIORITY_YELLOW, "yellow: " + how,
                           max_wait_s=self.yellow_wait):
            with self.lock:
                self.csv_events.append("say_yellow" if "ElevenLabs" in how
                                       else "say_yellow_fallback")

    def _get_tip(self):
        if self.request_tip is None:
            return None, "Gemini not connected"
        box, ev = {}, threading.Event()

        def done(tip, src):
            box.update(tip=tip, src=src)
            ev.set()
        try:
            self.request_tip("started getting louder", done)
        except Exception as e:
            return None, "error: %s" % e
        if not ev.wait(TIP_WAIT_S):
            return None, "Gemini took too long"
        return box.get("tip"), box.get("src", "")

    # =========================================================================
    # Tests (used by test_voice.py and GET /voice_test?play=...)
    # =========================================================================
    TEST_NAMES = ("tone", "speaker_test", "breathe_in", "hold", "breathe_out",
                  "breathing_done", "yellow_fallback", "red_fallback", "red", "yellow")

    def test_play(self, name):
        """Plays one thing right now, ignoring cooldowns. Returns a message."""
        if name == "yellow":
            with self.lock:
                self.stress = max(self.stress, self.yellow_at)
                self._yellow(time.time(), "test", force=True)
            return "yellow pipeline started (Gemini -> ElevenLabs -> speaker)"
        if name == "red":
            return "playing red message" if self._play_red("test red") else "no playable red message"
        if name not in FILES:
            return "unknown test sound"
        ok = self.audio.play(audio_file(name), am.PRIORITY_TEST, "test: " + name, max_wait_s=5)
        return ("playing " + FILES[name]) if ok else ("could not play " + FILES[name])

    # =========================================================================
    # Status
    # =========================================================================
    def parent_status(self):
        p = self.parent_path()
        if not p:
            return {"uploaded": False, "plays": "default_red.wav (fallback)"}
        ok, info = self.audio.check(p)
        out = {"uploaded": True, "file": os.path.basename(p), "playable": ok}
        if ok and info:
            out["seconds"] = round(info["seconds"], 1)
        if not ok:
            out["problem"] = info
        out["plays"] = out["file"] if ok else "default_red.wav (fallback)"
        return out

    def default_files_status(self):
        out = {}
        for name, f in FILES.items():
            ok, info = self.audio.check(audio_file(name))
            out[f] = ("ok, %.1f s" % info["seconds"]) if ok else "PROBLEM: %s" % info
        return out

    def status(self):
        now = time.time()
        with self.lock:
            return {
                "enabled": self.enabled,
                "modes": self.modes,
                "audio": self.audio.status(),
                "elevenlabs": {"key_set": elevenlabs_voice.is_configured(self.cfg),
                               "model": self.cfg.str("ELEVENLABS_MODEL_ID"),
                               "last_error": elevenlabs_voice.last_error()},
                "yellow": {"armed": self.yellow_armed,
                           "cooldown_left_s": max(0, round(self.yellow_cd - (now - self.last_yellow))),
                           "last_text": self.last_yellow_text},
                "red": {"cooldown_left_s": max(0, round(self.red_cd - (now - self.last_red)))},
                "breathing": self.breathing,
                "mic_muted_now": self.mic_muted(),
                "parent_recording": self.parent_status(),
                "upload_formats": self.cfg.list("PARENT_UPLOAD_FORMATS"),
                "upload_pin_required": bool(self.cfg.str("UPLOAD_PIN")),
                "default_files": self.default_files_status(),
                "events": list(self.events)[::-1][:20],
            }

    def _startup_report(self):
        a = self.audio.player
        print("[voice] speaker: %s  [%s]" % (a.description,
                                              getattr(a, "template", a.name)))
        if not a.is_real:
            print("[voice] NOTE: simulation - no sound will come out of the cube.")
        print("[voice] ElevenLabs key: %s" % ("set" if elevenlabs_voice.is_configured(self.cfg)
                                             else "NOT set -> yellow uses default_yellow.wav"))
        ps = self.parent_status()
        print("[voice] red message: %s" % ps["plays"] +
              ("  (uploaded file problem: %s)" % ps["problem"] if ps.get("problem") else ""))
        bad = [f for f, s in self.default_files_status().items() if s.startswith("PROBLEM")]
        if bad:
            print("[voice] WARNING: these audio files are missing or damaged: %s" % ", ".join(bad))
        if not self.enabled:
            print("[voice] voice is turned OFF (VOICE_ENABLED=false)")

    # =========================================================================
    # Web routes:  GET /voice_status   GET /voice_test?play=<name>
    # =========================================================================
    def handle_get(self, h, path, qs):
        if path not in ("/voice_status", "/voice_test"):
            return False
        import parent_audio_upload                       # for the LAN-only check
        if not parent_audio_upload.client_allowed(h, self.cfg):
            _reply(h, 403, {"error": "only devices on the cube's local network can do this"})
            return True
        if path == "/voice_status":
            _reply(h, 200, self.status())
            return True
        name = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("play", "")
        if name not in self.TEST_NAMES:
            _reply(h, 400, {"error": "play= must be one of: " + ", ".join(self.TEST_NAMES)})
            return True
        _reply(h, 200, {"ok": True, "result": self.test_play(name)})
        return True


def _reply(h, code, obj):
    body = json.dumps(obj, indent=1).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)
