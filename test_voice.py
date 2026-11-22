"""
Tests for the CoCube voice system. Each test can be run on its own.

ON THE CUBE (needs the speaker, asks you what you heard):
  python3 test_voice.py speaker      Test 1  test tone + voice through the speaker
  python3 test_voice.py breathing    Test 2  one breathing cycle, cues vs light timing
  python3 test_voice.py yellow       Test 3  Gemini -> ElevenLabs -> speaker, step by step
  python3 test_voice.py red          Test 4  the parent's uploaded recording
  python3 test_voice.py fallback     Test 5  pretends the internet is down

AUTOMATIC (no hardware, no internet, silent, prints PASS/FAIL):
  python3 test_voice.py cooldown     Test 6  staying loud doesn't make it talk again
  python3 test_voice.py priority     Test 7  breathing beats AI messages, red beats yellow
  python3 test_voice.py upload       security checks for parent uploads
  python3 test_voice.py existing     Test 8  starts calm_cube.py --sim and checks the
                                             web pages, button, modes and logging
  python3 test_voice.py all          every automatic test

Add  --audio-player sim  to any test to stay silent, or  --audio-player auto
to the automatic ones to hear them.
"""
import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import audio_manager as am          # noqa: E402
import elevenlabs_voice             # noqa: E402
import voice                        # noqa: E402
import voice_config                 # noqa: E402

RESULTS = []


def check(ok, what):
    RESULTS.append((bool(ok), what))
    print("  %s  %s" % ("PASS" if ok else "FAIL", what))
    return ok


def ask(question):
    try:
        return input("\n>>> %s [y/n] " % question).strip().lower().startswith("y")
    except EOFError:
        print("(no answer - counted as NO)")
        return False


def manager(player_kind, speed=1.0):
    cfg = voice_config.load()
    if player_kind == "sim":
        player = am.SimPlayer(speed=speed)
    else:
        player = am.make_player(cfg, player_kind)
    return am.AudioManager(player, cfg), cfg


def play_and_wait(audio, path, label, priority=am.PRIORITY_TEST, timeout=70):
    done, box = threading.Event(), {}

    def finished(result):
        box["result"] = result
        done.set()
    if not audio.play(path, priority, label, max_wait_s=5, on_done=finished):
        return "not played", audio.history[-1]["detail"] if audio.history else ""
    done.wait(timeout)
    last = [h for h in audio.history if h["label"] == label]
    return box.get("result", "timeout"), (last[-1]["detail"] if last else "")


def wait_quiet(audio, timeout=30):
    end = time.time() + timeout
    while time.time() < end and (audio.playing() or audio.pending):
        time.sleep(0.05)


def tone_wav(path, seconds, rate=22050):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00\xf0\xff" * int(seconds * rate / 2))


# =============================================================================
# Test 1 - speaker
# =============================================================================
def test_speaker(args):
    print("\nTEST 1 - SPEAKER")
    audio, cfg = manager(args.audio_player or "auto")
    p = audio.player
    print("Player: %s" % p.description)
    print("Command: %s" % getattr(p, "template", "(none - simulation)"))
    if not p.is_real:
        print("\nThis is SIMULATION, so nothing can be heard. On the cube, check that\n"
              "AUDIO_PLAYER=auto (or command) and that the player command exists:\n"
              "  which mmrplay")
    results = []
    for name in ("tone", "speaker_test"):
        print("\nPlaying %s ..." % voice.FILES[name])
        r, detail = play_and_wait(audio, voice.audio_file(name), "test: " + name)
        results.append(r)
        print("  player result: %s %s" % (r, detail))
    software_ok = all(r == "played" for r in results)
    if not p.is_real:
        print("\nSoftware side: SIMULATION - nothing was sent to a speaker.")
    else:
        print("\nSoftware side: %s" % ("the player command finished without errors" if software_ok
                                       else "the player reported a problem (see above)"))
    heard = p.is_real and ask("Did you hear a chime AND a voice from the speaker INSIDE the cube?")
    os.makedirs(am.CACHE_DIR, exist_ok=True)
    with open(am.SPEAKER_CHECK_FILE, "w") as fh:
        json.dump({"heard_by_person": heard, "player_ok": software_ok,
                   "command": getattr(p, "template", "sim"),
                   "time": time.strftime("%Y-%m-%d %H:%M:%S")}, fh)
    check(software_ok, "player command ran without errors")
    check(heard, "a person heard the sound from the cube's speaker")
    if not heard and p.is_real:
        print("\nIf the player was OK but you heard nothing: check the USB speaker/sound card\n"
              "is plugged in, its volume/amplifier is on, and try AUDIO_PLAY_CMD with\n"
              "'mmrplay -a alsa:USB {file}' in config.local. See README_VOICE_SETUP.md.")


# =============================================================================
# Test 2 - breathing cues in step with the light
# =============================================================================
def test_breathing(args):
    print("\nTEST 2 - BREATHING CUES (%d cycle%s)" % (args.cycles, "s" if args.cycles > 1 else ""))
    import calm_cube as cc
    audio, cfg = manager(args.audio_player or "auto")
    starts = []
    real_play = audio.player.play

    def timed_play(path, seconds, stop_event, info):
        starts.append((time.time(), os.path.basename(path)))
        return real_play(path, seconds, stop_event, info)
    audio.player.play = timed_play
    v = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True)
    lead = cfg.float("BREATHING_CUE_LEAD_S")
    cycle = cc.BREATHE_IN_S + cc.BREATHE_HOLD_S + cc.BREATHE_OUT_S
    total = cycle * args.cycles
    t0 = time.time()
    v.breathing_started()
    phase_times, last = [], None
    while time.time() - t0 < total:
        t = time.time() - t0
        ph = cc.breathe_phase(t)
        if ph != last:
            phase_times.append((t0 + t, ph[1]))
            last = ph
        if t + lead < total:              # this test may run fewer cycles than the cube
            v.breathing_phase(cc.breathe_phase(t + lead))
        b = cc.breathe_brightness(t) or 0
        sys.stdout.write("\r  %5.1f s  %-5s |%-40s|" % (t, ph[1], "#" * int(b * 40)))
        sys.stdout.flush()
        time.sleep(cc.FRAME_MS / 1000)
    v.breathing_stopped(finished=True)
    wait_quiet(audio)
    print("\n\n  light phase starts       cue started      difference")
    expected = {"in": "breathe_in.wav", "hold": "hold.wav", "out": "breathe_out.wav"}
    worst = 0.0
    for (tp, name), (ts, f) in zip(phase_times, starts):
        d = ts - tp
        worst = max(worst, abs(d))
        print("  %-6s at %6.2f s      %-16s %+.2f s" % (name, tp - t0, f, d))
        check(f == expected[name], "cue for '%s' is %s" % (name, expected[name]))
    check(len(starts) >= len(phase_times), "a cue started for every light phase")
    check(worst < 0.35, "cues start within 0.35 s of the light (worst %.2f s)" % worst)
    print("\n  This measures when the player STARTED. If the voice sounds late on the\n"
          "  cube, raise BREATHING_CUE_LEAD_S in config.local (e.g. 0.4).")
    if audio.player.is_real:
        check(ask("Did 'Breathe in' / 'Hold' / 'Breathe out' match the light bar above?"),
              "a person confirmed the cues matched the light")


# =============================================================================
# Test 3 - yellow: Gemini -> ElevenLabs -> speaker
# =============================================================================
def test_yellow(args):
    print("\nTEST 3 - YELLOW (Gemini -> ElevenLabs -> speaker)")
    audio, cfg = manager(args.audio_player or "auto")
    try:
        import gemini_tips
        t = time.time()
        tip, src = gemini_tips.get_tip(dict(mode="kid", why="started getting louder", stress=45,
                                            reds=0, bangs=1, yells=1, breaks=0,
                                            clock=time.strftime("%H:%M")))
        print("\n1. Gemini (%.1f s, source: %s):\n   %s" % (time.time() - t, src, tip))
        check(src == "gemini", "Gemini wrote the suggestion (source=%s)" % src)
    except ImportError:
        tip = "It's getting a little loud. Let's take a slow breath together."
        check(False, "gemini_tips.py found")
    path = None
    try:
        t = time.time()
        path, cached = elevenlabs_voice.synthesize(tip, cfg)
        info = am.wav_info(path)
        print("\n2. ElevenLabs: %s in %.1f s" % ("reused from cache" if cached else "generated",
                                                time.time() - t))
        check(True, "audio generation worked")
        check(os.path.exists(path), "audio file saved: %s (%.1f s)" % (path, info["seconds"]))
    except elevenlabs_voice.TTSError as e:
        print("\n2. ElevenLabs FAILED [%s]: %s" % (e.kind, e))
        check(False, "audio generation worked")
        print("   Playing the fallback instead.")
        path = voice.audio_file("yellow_fallback")
    print("\n3. Playing through: %s" % audio.player.description)
    r, detail = play_and_wait(audio, path, "test: yellow")
    check(r == "played", "player command finished OK (%s %s)" % (r, detail))
    if audio.player.is_real:
        check(ask("Did you hear the suggestion from the CUBE's speaker?"),
              "a person heard it from the cube")


# =============================================================================
# Test 4 - red: the parent's recording
# =============================================================================
def test_red(args):
    print("\nTEST 4 - RED (parent recording)")
    audio, cfg = manager(args.audio_player or "auto")
    try:
        import voice_messages
        parent = voice_messages.parent_voice_path()
    except ImportError:
        parent = None
    v = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True,
                          parent_path=lambda: parent)
    st = v.parent_status()
    print("Parent recording: %s" % json.dumps(st))
    if not check(st.get("uploaded"), "a parent recording has been uploaded"):
        print("  Upload one first at http://<cube-address>:8080/voice_upload")
    elif not check(st.get("playable"), "the uploaded recording is playable"):
        print("  Problem: %s" % st.get("problem"))
    v.test_play("red")
    time.sleep(0.3)
    label = audio.playing()[0] if audio.playing() else "(nothing)"
    print("Now playing: %s" % label)
    check("parent recording" in label, "the cube chose the parent's recording (not the default)")
    wait_quiet(audio, 70)
    last = audio.history[-1] if audio.history else {}
    check(last.get("result") == "played", "player command finished OK (%s)" % last.get("result"))
    if audio.player.is_real:
        check(ask("Did you hear the PARENT'S voice from the cube's speaker?"),
              "a person heard the parent's message from the cube")


# =============================================================================
# Test 5 - fallback when the internet is down
# =============================================================================
def test_fallback(args):
    print("\nTEST 5 - FALLBACK (pretending there is no internet)")
    audio, cfg = manager(args.audio_player or "sim", speed=1 if args.audio_player else 8)
    tmp = tempfile.mkdtemp()
    old_api, old_cache = elevenlabs_voice.API, elevenlabs_voice.CACHE_DIR
    elevenlabs_voice.API = "https://127.0.0.1:9/v1/text-to-speech/%s?output_format=pcm_%d"
    elevenlabs_voice.CACHE_DIR = tmp
    elevenlabs_voice._paused_until = 0
    if not cfg.str("ELEVENLABS_API_KEY"):
        cfg.set("ELEVENLABS_API_KEY", "test-key-not-real")
    gem = None
    try:
        import gemini_tips as gem
        old_gapi = gem.API
        gem.API = "https://127.0.0.1:9/v1beta/models"     # in memory only
    except ImportError:
        pass

    def tip_now(why, done):
        done(*(gem.get_tip(dict(mode="kid", why=why, stress=45, reds=0, bangs=0, yells=0,
                                breaks=0, clock="12:00")) if gem else (None, "none")))
    try:
        v = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True, request_tip=tip_now,
                              parent_path=lambda: None)
        t = time.time()
        v.update(t, 10, False, "kid", False)
        v.update(t + 0.1, 45, False, "kid", False)        # turns yellow
        deadline = time.time() + 25
        while time.time() < deadline and not any("yellow" in h["label"] for h in audio.history):
            time.sleep(0.1)
        wait_quiet(audio)
        ylab = [h for h in audio.history if "yellow" in h["label"]]
        check(ylab and "fallback" in ylab[-1]["label"] and ylab[-1]["file"] == "default_yellow.wav",
              "yellow used default_yellow.wav when offline")
        v.update(time.time(), 90, True, "kid", False)      # turns red, no parent file
        time.sleep(0.3)
        wait_quiet(audio)
        rlab = [h for h in audio.history if h["label"].startswith("red")]
        check(rlab and rlab[-1]["file"] == "default_red.wav",
              "red used default_red.wav when there is no parent recording")
        bad = os.path.join(tmp, "parent_voice.wav")
        with open(bad, "wb") as fh:
            fh.write(b"RIFF\x00\x00\x00\x00WAVEjunkjunkjunk")
        v2 = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True, parent_path=lambda: bad)
        v2.update(time.time(), 90, True, "kid", False)
        time.sleep(0.3)
        wait_quiet(audio)
        rlab = [h for h in audio.history if h["label"].startswith("red")]
        check(rlab and rlab[-1]["file"] == "default_red.wav",
              "a damaged parent recording falls back to default_red.wav (no crash)")
        check(elevenlabs_voice.last_error() is not None,
              "ElevenLabs error was caught: %s" % elevenlabs_voice.last_error())
    finally:
        elevenlabs_voice.API, elevenlabs_voice.CACHE_DIR = old_api, old_cache
        elevenlabs_voice._paused_until = 0
        if gem:
            gem.API = old_gapi
        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================
# Test 6 - cooldown
# =============================================================================
def test_cooldown(args):
    print("\nTEST 6 - COOLDOWN (simulated 7 minutes, runs in a few seconds)")
    audio, cfg = manager(args.audio_player or "sim", speed=200)
    cfg.set("YELLOW_COOLDOWN_S", "90")      # timeline below assumes long cooldowns
    cfg.set("RED_COOLDOWN_S", "120")
    calls = {"tip": 0, "tts": 0}

    def fake_tip(why, done):
        calls["tip"] += 1
        done("Let's take a slow breath together.", "test")

    def fake_tts(text, cfg):
        calls["tts"] += 1
        return voice.audio_file("yellow_fallback"), False
    v = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True, request_tip=fake_tip,
                          tts=fake_tts, parent_path=lambda: None)
    in_red = False

    def step(t, stress):
        nonlocal in_red
        turned = False
        if not in_red and stress >= 85:
            in_red = turned = True
        elif in_red and stress < 60:
            in_red = False
        v.update(t, stress, turned, "kid", False)
        time.sleep(0.004)

    t = 0.0
    while t < 200:                          # rise, then stay VERY loud for 3 minutes
        step(t, min(100, t * 10))
        t += 0.2
    time.sleep(0.5)
    check(calls["tip"] == 1, "staying loud for 3 minutes: Gemini asked once (asked %d)" % calls["tip"])
    check(calls["tts"] == 1, "...and ElevenLabs asked once (asked %d)" % calls["tts"])
    reds = sum(1 for h in audio.history if h["label"].startswith("red"))
    check(reds == 1, "...and the red message played once (played %d)" % reds)
    while t < 260:                          # wobble around the yellow line
        step(t, 38 + (8 if int(t * 5) % 2 else -8))
        t += 0.2
    time.sleep(0.3)
    check(calls["tip"] == 1, "wobbling around the yellow line doesn't repeat yellow")
    for _ in range(30):                     # calm down, then get loud again
        step(t, 5)
        t += 0.2
    while t < 300:
        step(t, min(100, (t - 266) * 10))
        t += 0.2
    time.sleep(0.5)
    check(calls["tip"] == 2, "after the room calmed down AND the cooldown passed, yellow can play again")
    reds = sum(1 for h in audio.history if h["label"].startswith("red"))
    check(reds == 2, "...and red can play again after its cooldown")
    for _ in range(30):                     # calm, loud again quickly: inside cooldown
        step(t, 5)
        t += 0.2
    t0 = t
    while t < t0 + 15:
        step(t, min(100, (t - t0) * 10))
        t += 0.2
    time.sleep(0.5)
    check(calls["tip"] == 2, "getting loud again inside the cooldown: no new yellow")
    reds = sum(1 for h in audio.history if h["label"].startswith("red"))
    check(reds == 2, "getting loud again inside the cooldown: no new red")


# =============================================================================
# Test 7 - priority
# =============================================================================
def test_priority(args):
    print("\nTEST 7 - PRIORITY")
    audio, cfg = manager(args.audio_player or "sim")
    tmp = tempfile.mkdtemp()
    long_yellow = os.path.join(tmp, "long_yellow.wav")
    tone_wav(long_yellow, 6)
    v = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True, parent_path=lambda: None)

    audio.play(long_yellow, am.PRIORITY_YELLOW, "yellow: AI message", max_wait_s=5)
    time.sleep(0.6)
    t = time.time()
    v.breathing_started()
    v.breathing_phase((0, "in"))
    while not (audio.playing() and "breathing" in audio.playing()[0]) and time.time() - t < 2:
        time.sleep(0.01)
    delay = time.time() - t
    y = [h for h in audio.history if h["label"] == "yellow: AI message"]
    check(y and y[-1]["result"] == "interrupted", "button breathing stops the AI message")
    check(delay < 0.3, "'Breathe in' starts %.2f s after the button (under 0.3 s)" % delay)
    v.update(time.time(), 95, True, "kid", False)
    time.sleep(0.2)
    check(not any(h["label"].startswith("red") for h in audio.history) and
          not (audio.playing() and audio.playing()[0].startswith("red")),
          "red message is NOT played during breathing")
    v.breathing_stopped()
    wait_quiet(audio)

    audio.play(long_yellow, am.PRIORITY_YELLOW, "yellow: second", max_wait_s=5)
    time.sleep(0.4)
    v.last_red = -1e9
    v.update(time.time(), 95, True, "kid", False)
    time.sleep(0.3)
    y = [h for h in audio.history if h["label"] == "yellow: second"]
    check(y and y[-1]["result"] == "interrupted", "red stops a yellow message")
    check(audio.playing() and "default red" in audio.playing()[0], "...and red plays instead")
    ok_wait = audio.play(long_yellow, am.PRIORITY_YELLOW, "yellow: during red", max_wait_s=0.5)
    wait_quiet(audio)
    y = [h for h in audio.history if h["label"] == "yellow: during red"]
    check(ok_wait and y and y[-1]["result"] == "dropped",
          "a yellow message that would play late (after red) is dropped")

    parent = os.path.join(tmp, "parent_voice.wav")
    tone_wav(parent, 1)
    v2 = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True, parent_path=lambda: parent)
    v2.update(time.time(), 95, True, "kid", False)
    time.sleep(0.2)
    check(audio.playing() and "parent recording" in audio.playing()[0],
          "the parent recording is used instead of the default red message")
    wait_quiet(audio)

    gate = threading.Event()

    def slow_tip(why, done):
        threading.Thread(target=lambda: (gate.wait(3), done("Slow tip.", "test"))).start()
    v3 = voice.VoiceSystem(cfg=cfg, audio=audio, quiet=True, request_tip=slow_tip,
                           tts=lambda text, cfg: (long_yellow, False), parent_path=lambda: None)
    v3.update(time.time(), 50, False, "kid", False)        # yellow starts preparing
    time.sleep(0.1)
    v3.breathing_started()                                  # child presses the button
    gate.set()
    time.sleep(0.5)
    check(not any(h["label"].startswith("yellow: ElevenLabs") for h in audio.history),
          "a yellow message still being prepared is thrown away when breathing starts")
    v3.breathing_stopped()
    wait_quiet(audio)
    shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================
# Upload security
# =============================================================================
def test_upload(args):
    print("\nUPLOAD SECURITY")
    import parent_audio_upload as pu
    from http.server import BaseHTTPRequestHandler, HTTPServer
    tmp = tempfile.mkdtemp()
    old = pu.FOLDER
    pu.FOLDER = tmp
    cfg = voice_config.load()
    cfg.set("UPLOAD_PIN", "")
    cfg.set("PARENT_UPLOAD_FORMATS", "wav")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            path, _, qs = self.path.partition("?")
            if not pu.handle_post(self, path, qs, cfg):
                self.send_response(404)
                self.end_headers()
    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(body, name="x.wav", ctype="audio/wav", extra=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Content-Type": ctype, "X-File-Name": name}
        h.update(extra or {})
        try:
            c.request("POST", "/voice?zone=red", body=body, headers=h)
            r = c.getresponse()
            return r.status, r.read().decode(errors="replace")
        except (ConnectionError, OSError) as e:
            return 0, str(e)

    good = os.path.join(tmp, "src.wav")
    tone_wav(good, 2)
    data = open(good, "rb").read()
    os.remove(good)
    code, _ = post(data, name="../../../etc/passwd.wav")
    check(code == 200, "a normal WAV recording is accepted")
    files = sorted(os.listdir(tmp))
    check(files == ["parent_voice.json", "parent_voice.wav"],
          "saved only as parent_voice.wav (+json), whatever name was sent: %s" % files)
    mode = os.stat(os.path.join(tmp, "parent_voice.wav")).st_mode
    check(mode & 0o111 == 0, "saved file is not executable")
    meta = json.load(open(os.path.join(tmp, "parent_voice.json")))
    check(meta == {"file": "parent_voice.wav", "type": "audio/wav"},
          "metadata matches what voice_messages.py expects")
    code, _ = post(b"\x7fELF\x02\x01\x01" + b"\x00" * 5000, name="evil.wav")
    check(code == 415, "a program (ELF) disguised as audio/wav is refused (%d)" % code)
    code, _ = post(b"#!/bin/sh\nrm -rf /\n" + b" " * 3000, name="song.wav")
    check(code == 415, "a shell script is refused (%d)" % code)
    code, _ = post(b"MZ" + b"\x00" * 3000, name="a.mp3", ctype="audio/mpeg")
    check(code == 415, "a Windows program is refused (%d)" % code)
    code, body = post(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 3000, name="memo.m4a",
                      ctype="audio/mp4")
    check(code == 415 and "voice_upload" in body,
          "an m4a is refused with a pointer to /voice_upload while only WAV is enabled")
    code, _ = post(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x01" * 3000)
    check(code == 415, "a damaged WAV is refused (%d)" % code)
    long = os.path.join(tmp, "long.wav")
    tone_wav(long, 62, rate=8000)
    code, _ = post(open(long, "rb").read())
    os.remove(long)
    check(code == 413, "a recording over 60 s is refused (%d)" % code)
    code, _ = post(b"0" * (pu.MAX_BYTES + 10))
    check(code in (413, 0), "a file over 8 MB is refused (%d)" % code)
    check(sorted(os.listdir(tmp)) == ["parent_voice.json", "parent_voice.wav"],
          "refused uploads left no files behind and kept the good recording")
    cfg.set("UPLOAD_PIN", "4321")
    code, _ = post(data)
    check(code == 403, "with UPLOAD_PIN set, an upload without the PIN is refused")
    code, _ = post(data, extra={"X-Upload-Pin": "4321"})
    check(code == 200, "...and with the right PIN it is accepted")
    cfg.set("UPLOAD_PIN", "")

    class Fake:
        def __init__(self, ip):
            self.client_address = (ip, 1234)
            self.connection = None
    for ip, want in (("192.168.1.20", True), ("10.0.0.5", True), ("127.0.0.1", True),
                     ("::ffff:172.16.3.4", True), ("8.8.8.8", False), ("52.95.110.1", False)):
        check(pu.client_allowed(Fake(ip), cfg) == want,
              "upload from %s %s" % (ip, "allowed" if want else "refused"))
    srv.shutdown()
    pu.FOLDER = old
    shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================
# Test 8 - existing features still work (runs calm_cube.py --sim)
# =============================================================================
def test_existing(args):
    print("\nTEST 8 - EXISTING FEATURES (calm_cube.py --sim on port 18080, silent)")
    tmp = tempfile.mkdtemp()
    code = ("import sys; sys.path.insert(0, %r); import calm_cube as c; "
            "c.WEB_PORT = 18080; c.LOG_FILE = 'calm_cube_log.csv'; c.main()" % HERE)
    p = subprocess.Popen([sys.executable, "-u", "-c", code, "--sim", "--audio-player", "sim"],
                         cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    out = []
    threading.Thread(target=lambda: out.extend(p.stdout), daemon=True).start()

    def key(k):
        p.stdin.write(k + "\n")
        p.stdin.flush()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", 18080, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()

    def status():
        return json.loads(get("/status")[1])
    try:
        time.sleep(4.5)                                    # calibration takes ~3 s
        s = status()
        check(not s["calibrating"], "calibration finished")
        check(get("/")[0] == 200 and b"CoCube" in get("/")[1], "dashboard page loads")
        check("voices" in s and "speaking" in s, "/status still has the dashboard fields")
        check(get("/voice_upload")[0] == 200, "/voice_upload page loads")
        check(get("/voice_status")[0] == 200, "/voice_status works")
        key("1")
        time.sleep(1.5)
        vs = json.loads(get("/voice_status")[1])
        check(any("yellow" in e["text"] for e in vs["events"]), "yellow detected and handled")
        key("p")
        time.sleep(1.0)
        check(status()["breathing"], "short press starts breathing")
        vs = json.loads(get("/voice_status")[1])
        check(any("breathing" in h["label"] for h in vs["audio"]["recent"]) or
              "breathing" in (vs["audio"]["playing_now"] or ""), "breathing cue was played")
        key("p")
        time.sleep(0.6)
        check(not status()["breathing"], "short press again stops breathing")
        key("l")
        time.sleep(3.0)
        check(status()["break_alert"], "long press sends the break request to the dashboard")
        get("/ack")
        time.sleep(0.4)
        check(not status()["break_alert"], "/ack clears the break request")
        reds = status()["reds"]
        key("2")
        time.sleep(1.0)
        check(status()["reds"] == reds + 1, "red counted")
        vs = json.loads(get("/voice_status")[1])
        check(any(h["label"].startswith("red") for h in vs["audio"]["recent"]) or
              (vs["audio"]["playing_now"] or "").startswith("red"), "red message played")
        get("/mode?m=focus")
        time.sleep(0.3)
        check(status()["mode"] == "kid", "unknown mode is ignored")
        code_, body = get("/report/summary.csv")
        check(code_ == 200 and b"Date" in body, "CSV report downloads")
        time.sleep(1.2)
        log = open(os.path.join(tmp, "calm_cube_log.csv")).read()
        check("red" in log and "breathe" in log and "say_red" in log,
              "CSV log has events, including voice events")
    except Exception as e:
        check(False, "test crashed: %s" % e)
    finally:
        key("q")
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
        text = "".join(out)
        check("Traceback" not in text, "no Python errors in the cube's output")
        if "Traceback" in text:
            print(text[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)


TESTS = {"speaker": test_speaker, "breathing": test_breathing, "yellow": test_yellow,
         "red": test_red, "fallback": test_fallback, "cooldown": test_cooldown,
         "priority": test_priority, "upload": test_upload, "existing": test_existing}
AUTOMATIC = ["cooldown", "priority", "fallback", "upload", "existing"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("test", choices=list(TESTS) + ["all"])
    ap.add_argument("--audio-player", choices=["auto", "command", "sim", "laptop"])
    ap.add_argument("--cycles", type=int, default=1, help="breathing cycles for Test 2")
    args = ap.parse_args(argv)
    names = AUTOMATIC if args.test == "all" else [args.test]
    if all(n in AUTOMATIC for n in names):
        # These tests assert immediately, so skip the playback delay
        os.environ.setdefault("SOUND_DELAY_S", "0")
    for n in names:
        TESTS[n](args)
    passed = sum(1 for ok, _ in RESULTS if ok)
    print("\n%d of %d checks passed." % (passed, len(RESULTS)))
    for ok, what in RESULTS:
        if not ok:
            print("  FAILED: " + what)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
