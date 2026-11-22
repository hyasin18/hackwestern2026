#!/usr/bin/env python3
"""
Calm Cube - a desk cube that glows calm when it's quiet and shifts toward red
when you yell, rage, or bang the table. Press the button for a breathing
exercise; hold it for 2 s to send an "I need a break" alert.

Runs on QNX 8 (Raspberry Pi 5 quick start image) with only the Python
standard library + QNX's rpi_gpio module. Also runs on a laptop with no
hardware (LEDs are drawn in the terminal) so you can develop in parallel.

  python3 calm_cube.py --test-mic          # step 1: is the mic working?
  python3 calm_cube.py --test-leds         # step 2: are the LEDs wired right?
  python3 calm_cube.py                     # run the cube (kid mode)
  python3 calm_cube.py --mode gamer        # rage-meter mode
  python3 calm_cube.py --sim               # no mic/LEDs: type b / y / p / l
"""

import argparse
import array
import json
import math
import os
import queue
import shlex
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# =============================================================================
# SETTINGS - change these first
# =============================================================================

# GPIO pins (BCM numbering). Physical header pin in brackets.
PIN_RED = 17      # [pin 11]
PIN_GREEN = 27    # [pin 13]
PIN_BLUE = 22     # [pin 15]
PIN_BUTTON = 23   # [pin 16]  other button leg -> GND [pin 14 or 20]

COMMON_ANODE = False   # True if your RGB LED's long leg goes to 3.3V, not GND
PWM_FREQ_HZ = 200      # LED PWM frequency

# Audio. The command must write audio to stdout (raw 16-bit or a WAV stream).
SAMPLE_RATE = 16000
AUDIO_CMD = "arecord -f S16_LE -r 16000 -c 1 -t raw -"
# If arecord on your image can't write to stdout, use chunk mode
# (--chunk): the command records ~1 s to a file, then we read it.
CHUNK_CMD = "arecord -d 1 /tmp/calm_cube_chunk.wav"
CHUNK_FILE = "/tmp/calm_cube_chunk.wav"

WEB_PORT = 8080
LOG_FILE = "calm_cube_log.csv"

FRAME_MS = 20          # loudness is measured every 20 ms (50x per second)

# Per-mode behaviour. dB values are "above the room's normal noise level".
MODES = {
    # Soft colours, no flashes, slow fades. Auto-starts breathing when high.
    "kid": dict(loud_db=14, bang_db=20, yell_rate=18, bang_add=10,
                decay=5, fade_s=1.5, flash=False, auto_breathe=True,
                palette=[(0, (0, 180, 120)), (40, (120, 200, 0)),
                         (70, (255, 140, 0)), (100, (200, 40, 120))]),
    # Nudges you sooner when the room gets noisy.
    "focus": dict(loud_db=10, bang_db=18, yell_rate=12, bang_add=8,
                  decay=4, fade_s=1.0, flash=False, auto_breathe=False,
                  palette=[(0, (0, 120, 255)), (50, (255, 180, 0)),
                           (100, (255, 60, 0))]),
    # Rage meter: fast, flashes on table bangs, goes full red.
    "gamer": dict(loud_db=16, bang_db=22, yell_rate=30, bang_add=18,
                  decay=7, fade_s=0.25, flash=True, auto_breathe=False,
                  palette=[(0, (0, 255, 60)), (35, (255, 220, 0)),
                           (65, (255, 90, 0)), (100, (255, 0, 0))]),
}

BREATHE_IN_S, BREATHE_HOLD_S, BREATHE_OUT_S = 4.0, 2.0, 6.0
BREATHE_CYCLES = 5                     # 5 x 12 s = 1 minute
BREATHE_COLOR = (40, 90, 255)
BREAK_COLOR = (150, 60, 255)
LONG_PRESS_S = 2.0
AUTO_BREATHE_STRESS, AUTO_BREATHE_HOLD_S = 80, 4.0

# =============================================================================
# Audio input
# =============================================================================


def frame_stats(samples):
    """Return (rms_dbfs, peak_dbfs) for a sequence of int16 samples."""
    if not samples:
        return -96.0, -96.0
    acc = 0
    peak = 0
    for s in samples:
        acc += s * s
        a = -s if s < 0 else s
        if a > peak:
            peak = a
    rms = math.sqrt(acc / len(samples))
    to_db = lambda v: 20 * math.log10(max(v, 1) / 32768.0)
    return to_db(rms), to_db(peak)


def parse_wav_header(read):
    """Given a read(n) function positioned after 'RIFF', skip to the data
    chunk. Returns (sample_rate, channels, bits)."""
    read(8)  # size + 'WAVE'
    rate, ch, bits = SAMPLE_RATE, 1, 16
    while True:
        hdr = read(8)
        if len(hdr) < 8:
            raise EOFError("WAV header ended early")
        cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
        if cid == b"data":
            return rate, ch, bits
        body = read(size + (size & 1))
        if cid == b"fmt ":
            _, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])


class AudioSource(threading.Thread):
    """Reads audio from a command and pushes (rms_db, peak_db) per frame."""

    def __init__(self, out_q, chunk_mode=False, cmd=None):
        super().__init__(daemon=True)
        self.q = out_q
        self.chunk_mode = chunk_mode
        self.cmd = cmd or (CHUNK_CMD if chunk_mode else AUDIO_CMD)
        self.error = None

    def _emit_pcm(self, data, rate, ch, bits, leftover):
        """Turn raw bytes into frames. Returns unused bytes."""
        if bits != 16:
            raise ValueError("Only 16-bit audio supported; got %d-bit" % bits)
        data = leftover + data
        frame_bytes = int(rate * FRAME_MS / 1000) * ch * 2
        n = len(data) // frame_bytes
        for i in range(n):
            chunk = data[i * frame_bytes:(i + 1) * frame_bytes]
            samples = array.array("h")
            samples.frombytes(chunk)
            if sys.byteorder == "big":
                samples.byteswap()
            if ch > 1:
                samples = samples[::ch]  # first channel only
            self.q.put(frame_stats(samples))
        return data[n * frame_bytes:]

    def run(self):
        try:
            if self.chunk_mode:
                self._run_chunks()
            else:
                self._run_stream()
        except Exception as e:  # shown by the main loop
            self.error = e

    def _run_stream(self):
        proc = subprocess.Popen(shlex.split(self.cmd), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        f = proc.stdout
        rate, ch, bits = SAMPLE_RATE, 1, 16
        head = f.read(4)
        leftover = b""
        if head == b"RIFF":
            rate, ch, bits = parse_wav_header(f.read)
        else:
            leftover = head
        while True:
            data = f.read(1024)
            if not data:
                err = proc.stderr.read().decode(errors="replace").strip()
                raise RuntimeError("Audio command stopped. %s" % err)
            leftover = self._emit_pcm(data, rate, ch, bits, leftover)

    def _run_chunks(self):
        while True:
            if os.path.exists(CHUNK_FILE):
                os.remove(CHUNK_FILE)
            r = subprocess.run(shlex.split(self.cmd), capture_output=True)
            if not os.path.exists(CHUNK_FILE):
                raise RuntimeError("Chunk command made no file. %s" %
                                   r.stderr.decode(errors="replace").strip())
            with open(CHUNK_FILE, "rb") as fh:
                if fh.read(4) != b"RIFF":
                    raise RuntimeError("Chunk file is not a WAV file")
                rate, ch, bits = parse_wav_header(fh.read)
                self._emit_pcm(fh.read(), rate, ch, bits, b"")


class SimSource(threading.Thread):
    """Fake mic for laptops. Type a letter + Enter:
    b = table bang, y = 2 s of yelling, n = 3 s of loud talking,
    p = short button press, l = long press (break alert), q = quit."""

    def __init__(self, out_q, button):
        super().__init__(daemon=True)
        self.q, self.button = out_q, button
        self.script = []
        self.lock = threading.Lock()
        self.error = None
        threading.Thread(target=self._keys, daemon=True).start()

    def _keys(self):
        print(__doc__ and SimSource.__doc__)
        for line in sys.stdin:
            k = line.strip().lower()[:1]
            with self.lock:
                if k == "b":
                    self.script += [(-15, -2)] * 3
                elif k == "y":
                    self.script += [(-12, -6)] * 100
                elif k == "n":
                    self.script += [(-30, -22)] * 150
            if k == "p":
                self.button.sim_press(0.2)
            elif k == "l":
                self.button.sim_press(LONG_PRESS_S + 0.2)
            elif k == "q":
                os._exit(0)

    def run(self):
        import random
        while True:
            time.sleep(FRAME_MS / 1000)
            with self.lock:
                v = self.script.pop(0) if self.script else None
            self.q.put(v or (-50 + random.uniform(-2, 2), -40))


# =============================================================================
# Bang / yell detection and the stress meter
# =============================================================================


class Detector:
    """Feed it one (rms_db, peak_db) per frame. Tracks the room's normal
    noise level and a 0-100 stress value."""

    def __init__(self, mode):
        self.cfg = MODES[mode]
        self.floor = None          # room's normal noise level (dBFS)
        self.calib = []
        self.loud_run = 0          # consecutive loud frames
        self.run_peak = -96.0
        self.quiet_s = 0.0
        self.stress = 0.0
        self.level = -96.0
        self.events = []           # ("bang"|"yell", time) since last read

    def set_mode(self, mode):
        self.cfg = MODES[mode]

    @property
    def calibrating(self):
        return self.floor is None

    def feed(self, rms_db, peak_db):
        dt = FRAME_MS / 1000
        self.level = rms_db
        if self.floor is None:                       # first ~3 s: learn room
            self.calib.append(rms_db)
            if len(self.calib) >= 150:
                s = sorted(self.calib)
                self.floor = s[len(s) // 2]
            return
        above = rms_db - self.floor
        c = self.cfg
        loud = above >= c["loud_db"]
        if loud:
            self.loud_run += 1
            self.run_peak = max(self.run_peak, peak_db - self.floor)
            self.quiet_s = 0.0
            if self.loud_run * FRAME_MS >= 300:      # sustained = yelling
                self.stress += c["yell_rate"] * dt
                if self.loud_run * FRAME_MS == 600:
                    self.events.append(("yell", time.time()))
        else:
            # A short loud burst that just ended = a bang
            if 0 < self.loud_run * FRAME_MS <= 100 and \
                    self.run_peak >= c["bang_db"]:
                self.stress += c["bang_add"]
                self.events.append(("bang", time.time()))
            self.loud_run, self.run_peak = 0, -96.0
            # Medium noise (talking loudly) creeps the meter up slowly
            if above >= c["loud_db"] * 0.6:
                self.stress += c["yell_rate"] * 0.15 * dt
                self.quiet_s = 0.0
            else:
                self.quiet_s += dt
                if self.quiet_s > 1.5:               # calm down after 1.5 s
                    self.stress -= c["decay"] * dt
                # Slowly follow the room's noise level when it's quiet
                if above < 6:
                    self.floor += (rms_db - self.floor) * 0.002
        self.stress = max(0.0, min(100.0, self.stress))

    def pop_events(self):
        ev, self.events = self.events, []
        return ev


# =============================================================================
# LEDs
# =============================================================================

try:
    import rpi_gpio as GPIO      # QNX
    HAVE_GPIO = True
except ImportError:
    try:
        import RPi.GPIO as GPIO  # Raspberry Pi OS (for testing)
        HAVE_GPIO = True
    except ImportError:
        GPIO = None
        HAVE_GPIO = False


def gamma(v):
    """0-255 colour value -> 0-100 duty cycle, gamma-corrected so fades
    look smooth to the eye."""
    return 100.0 * (max(0, min(255, v)) / 255.0) ** 2.2


class Leds:
    def __init__(self, use_gpio):
        self.use_gpio = use_gpio
        self.pwm = None
        self.last = None
        self.last_draw = 0
        if use_gpio:
            GPIO.setmode(GPIO.BCM)
            for p in (PIN_RED, PIN_GREEN, PIN_BLUE):
                GPIO.setup(p, GPIO.OUT)
            try:
                self.pwm = [GPIO.PWM(p, PWM_FREQ_HZ)
                            for p in (PIN_RED, PIN_GREEN, PIN_BLUE)]
                for p in self.pwm:
                    p.start(100.0 if COMMON_ANODE else 0.0)
            except Exception as e:
                print("PWM not available (%s) - using on/off colours" % e)
                self.pwm = None

    def set(self, rgb):
        rgb = tuple(int(max(0, min(255, c))) for c in rgb)
        if self.use_gpio:
            if rgb == self.last:
                return
            self.last = rgb
            if self.pwm:
                for p, c in zip(self.pwm, rgb):
                    dc = gamma(c)
                    p.ChangeDutyCycle(100.0 - dc if COMMON_ANODE else dc)
            else:
                for pin, c in zip((PIN_RED, PIN_GREEN, PIN_BLUE), rgb):
                    on = (c > 100) != COMMON_ANODE
                    GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)
        else:
            self.last = rgb

    def draw_terminal(self, text):
        """Show the colour as a block in the terminal (laptop mode)."""
        now = time.time()
        if now - self.last_draw < 0.1:
            return
        self.last_draw = now
        r, g, b = self.last or (0, 0, 0)
        sys.stdout.write("\r\x1b[48;2;%d;%d;%dm%s\x1b[0m %s\x1b[K" %
                         (r, g, b, " " * 12, text))
        sys.stdout.flush()

    def off(self):
        self.set((0, 0, 0))
        if self.use_gpio:
            try:
                if self.pwm:
                    for p in self.pwm:
                        p.stop()
                GPIO.cleanup()
            except Exception:
                pass


def palette_color(palette, x):
    for (x0, c0), (x1, c1) in zip(palette, palette[1:]):
        if x <= x1:
            t = (x - x0) / float(x1 - x0) if x1 > x0 else 0
            return tuple(a + (b - a) * t for a, b in zip(c0, c1))
    return palette[-1][1]


# =============================================================================
# Button
# =============================================================================


class Button:
    """Polled button with debounce. Reports 'short' or 'long' presses."""

    def __init__(self, use_gpio):
        self.use_gpio = use_gpio
        self.down_since = None
        self.long_sent = False
        self.stable = False
        self.raw_since = 0
        self.sim_until = 0
        if use_gpio:
            GPIO.setup(PIN_BUTTON, GPIO.IN, GPIO.PUD_UP)

    def sim_press(self, seconds):
        self.sim_until = time.time() + seconds

    def _pressed(self):
        if time.time() < self.sim_until:
            return True
        if self.use_gpio:
            return GPIO.input(PIN_BUTTON) == GPIO.LOW   # pull-up: LOW = down
        return False

    def poll(self):
        now = time.time()
        p = self._pressed()
        if p != self.stable:
            if self.raw_since == 0:
                self.raw_since = now
            if now - self.raw_since < 0.03:             # 30 ms debounce
                return None
            self.stable, self.raw_since = p, 0
            if p:
                self.down_since, self.long_sent = now, False
                return None
            held = now - (self.down_since or now)
            if not self.long_sent and held < LONG_PRESS_S:
                return "short"
            return None
        self.raw_since = 0
        if p and not self.long_sent and self.down_since and \
                now - self.down_since >= LONG_PRESS_S:
            self.long_sent = True
            return "long"
        return None


# =============================================================================
# Shared state + web page
# =============================================================================

RED_AT, RED_RESET = 85, 60   # "turned red" counts when stress crosses 85
ZONES = [("calm", 35), ("rising", 65), ("high", 85), ("red", 101)]


def zone_of(stress):
    for name, top in ZONES:
        if stress < top:
            return name
    return "red"


def fresh_counts():
    return {"reds": 0, "bangs": 0, "yells": 0, "breaks": 0, "breaths": 0,
            "zone_s": {z: 0.0 for z, _ in ZONES}, "history": [],
            "events": [], "since": time.time()}


STATE = {"mode": "kid", "stress": 0, "level_db": -96, "floor_db": None,
         "color": [0, 0, 0], "zone": "calm", "breathing": False,
         "break_alert": None, "calibrating": True,
         "tip": "Tips appear here when the cube turns red.",
         "tip_source": "", "tip_time": None, "tip_loading": False}
STATE.update(fresh_counts())
STATE_LOCK = threading.RLock()
COMMANDS = queue.Queue()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import gemini_tips          # Eliza's file, next to this one
except ImportError:
    gemini_tips = None
    print("gemini_tips.py not found - no calming tips")


def add_event(kind, text):
    """Event list for the dashboard (newest first, last 40)."""
    STATE["events"].insert(0, {"t": time.time(), "kind": kind,
                               "text": text})
    del STATE["events"][40:]


def request_tip(why):
    """Ask Gemini for a tip in the background so the lights never pause."""
    if gemini_tips is None:
        return
    with STATE_LOCK:
        if STATE["tip_loading"]:
            return
        STATE["tip_loading"] = True
        ctx = dict(mode=STATE["mode"], why=why, stress=STATE["stress"],
                   reds=STATE["reds"], bangs=STATE["bangs"],
                   yells=STATE["yells"], breaks=STATE["breaks"],
                   clock=time.strftime("%H:%M"))

    def work():
        try:
            tip, src = gemini_tips.get_tip(ctx)
        except Exception as e:
            tip, src = "Take three slow breaths with the cube.", "offline"
            print("\nTip error: %s" % e)
        with STATE_LOCK:
            STATE.update(tip=tip, tip_source=src, tip_time=time.time(),
                         tip_loading=False)
    threading.Thread(target=work, daemon=True).start()

PAGE = """<!doctype html><html><head><meta name=viewport
content="width=device-width,initial-scale=1"><title>Calm Cube</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:20px;
background:#111;color:#eee;text-align:center}#c{width:160px;height:160px;
margin:20px auto;border-radius:24px;transition:background .3s}
#alert{display:none;background:#7a3cff;padding:16px;border-radius:12px;
font-size:1.3em;margin:12px 0}button{font-size:1em;padding:10px 16px;
margin:4px;border-radius:8px;border:0}.bar{height:14px;background:#333;
border-radius:7px;overflow:hidden}.bar div{height:100%;background:#f80}
</style></head><body><h2>Calm Cube</h2><div id=alert>I need a break
<span id=at></span><br><button onclick="go('/ack')">OK, coming</button></div>
<div id=c></div><div class=bar><div id=s></div></div><p id=t></p>
<p><button onclick="go('/mode?m=kid')">Kid</button>
<button onclick="go('/mode?m=focus')">Focus</button>
<button onclick="go('/mode?m=gamer')">Gamer</button>
<button onclick="go('/breathe')">Start breathing</button></p>
<p>Times turned red: <b id=r>0</b></p>
<script>
function go(u){fetch(u).then(tick)}
function tick(){fetch('/status').then(r=>r.json()).then(s=>{
document.getElementById('c').style.background='rgb('+s.color.join(',')+')';
document.getElementById('s').style.width=s.stress+'%';
document.getElementById('t').textContent='Mode: '+s.mode+' | stress '+
Math.round(s.stress)+' | bangs '+s.bangs+' | yells '+s.yells+
(s.breathing?' | breathing...':'');document.getElementById('r').textContent=s.reds;
var a=document.getElementById('alert');
if(s.break_alert){a.style.display='block';document.getElementById('at')
.textContent='('+new Date(s.break_alert*1000).toLocaleTimeString()+')'}
else a.style.display='none'})}
setInterval(tick,500);tick();
</script></body></html>"""


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/":
            # Eliza's dashboard.html if it's next to this file, else a basic page
            dash = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "dashboard.html")
            if os.path.exists(dash):
                with open(dash, encoding="utf-8") as fh:
                    return self._send(fh.read(), "text/html; charset=utf-8")
            return self._send(PAGE, "text/html")
        if path == "/status":
            with STATE_LOCK:
                return self._send(json.dumps(STATE))
        if path == "/mode" and qs.startswith("m="):
            COMMANDS.put(("mode", qs[2:]))
        elif path == "/tip":
            request_tip("asked for a tip on the dashboard")
        elif path in ("/ack", "/breathe", "/break", "/reset"):
            COMMANDS.put((path[1:], None))
        self._send('{"ok":true}')


def start_web():
    try:
        srv = HTTPServer(("0.0.0.0", WEB_PORT), Web)
    except OSError as e:
        print("Web page disabled: %s" % e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("Web page: http://<pi-ip-address>:%d" % WEB_PORT)


# =============================================================================
# Main loop
# =============================================================================


def breathe_brightness(t):
    """0..1 brightness for breathing at time t (seconds since start).
    Returns None when the exercise is over."""
    cycle = BREATHE_IN_S + BREATHE_HOLD_S + BREATHE_OUT_S
    if t >= cycle * BREATHE_CYCLES:
        return None
    t %= cycle
    if t < BREATHE_IN_S:
        x = t / BREATHE_IN_S
    elif t < BREATHE_IN_S + BREATHE_HOLD_S:
        x = 1.0
    else:
        x = 1.0 - (t - BREATHE_IN_S - BREATHE_HOLD_S) / BREATHE_OUT_S
    return 0.08 + 0.92 * (0.5 - 0.5 * math.cos(math.pi * x))


def run(args):
    use_gpio = HAVE_GPIO and not args.sim
    if not use_gpio:
        print("No GPIO found - showing the cube colour in this terminal.")
    leds = Leds(use_gpio)
    button = Button(use_gpio)
    det = Detector(args.mode)
    STATE["mode"] = args.mode
    frames = queue.Queue()
    src = SimSource(frames, button) if args.sim else \
        AudioSource(frames, chunk_mode=args.chunk, cmd=args.audio_cmd)
    src.start()
    start_web()

    log = open(LOG_FILE, "a")
    if log.tell() == 0:
        log.write("time,level_db,floor_db,stress,mode,event\n")
    color = [0.0, 0.0, 0.0]
    breathe_t0 = None
    break_alert = None
    high_since = None
    flash_until = 0
    last_log = 0
    print("Calibrating to the room for 3 seconds - keep it normal...")

    in_red = False
    last_hist = 0
    log_events = []
    try:
        while True:
            tick = time.time()
            dt = FRAME_MS / 1000
            if src.error:
                raise SystemExit("\nAudio problem: %s\nTry --test-mic, "
                                 "--chunk, or --audio-cmd." % src.error)
            # 1. Audio frames -> detector
            while True:
                try:
                    det.feed(*frames.get_nowait())
                except queue.Empty:
                    break
            events = [k for k, _ in det.pop_events()]
            for kind in events:
                if kind == "bang" and det.cfg["flash"]:
                    flash_until = tick + 0.15

            # 2. Button + web commands -> actions
            actions = []
            press = button.poll()
            if press == "short":
                actions.append("stop_breathe" if breathe_t0 else "breathe")
            elif press == "long":
                actions.append("ack" if break_alert else "break")
            while True:
                try:
                    cmd, val = COMMANDS.get_nowait()
                except queue.Empty:
                    break
                if cmd == "mode" and val in MODES:
                    det.set_mode(val)
                    with STATE_LOCK:
                        STATE["mode"] = val
                        add_event("mode", "Switched to %s mode" % val)
                else:
                    actions.append(cmd)

            # 3. Auto-start breathing when stress stays high (kid mode)
            if det.cfg["auto_breathe"] and det.stress >= AUTO_BREATHE_STRESS:
                high_since = high_since or tick
                if not breathe_t0 and tick - high_since > AUTO_BREATHE_HOLD_S:
                    actions.append("auto_breathe")
            else:
                high_since = None

            # 4. "Turned red" = stress crosses RED_AT (resets below RED_RESET)
            turned_red = False
            if not in_red and det.stress >= RED_AT:
                in_red, turned_red = True, True
            elif in_red and det.stress < RED_RESET:
                in_red = False

            # 5. Update the shared numbers the dashboard shows
            want_tip = None
            with STATE_LOCK:
                for kind in events:
                    STATE["bangs" if kind == "bang" else "yells"] += 1
                    add_event(kind, "Table bang" if kind == "bang"
                              else "Yelling / loud for a while")
                if turned_red:
                    STATE["reds"] += 1
                    add_event("red", "Turned red (#%d)" % STATE["reds"])
                    want_tip = "turned red"
                for a in actions:
                    if a in ("breathe", "auto_breathe"):
                        breathe_t0 = tick
                        STATE["breaths"] += 1
                        add_event("breathe", "Breathing started" +
                                  (" automatically" if a == "auto_breathe"
                                   else ""))
                    elif a == "stop_breathe":
                        breathe_t0 = None
                    elif a == "break" and not break_alert:
                        break_alert = tick
                        STATE["breaks"] += 1
                        add_event("break", "Asked for a break")
                        want_tip = "asked for a break"
                        print("\n*** I NEED A BREAK ***")
                    elif a == "ack" and break_alert:
                        break_alert = None
                        add_event("ack", "Break answered")
                    elif a == "reset":
                        STATE.update(fresh_counts())
                        add_event("reset", "Counts reset")
                    if a != "stop_breathe":
                        log_events.append(a)
                log_events += events + (["red"] if turned_red else [])
                if not det.calibrating:
                    STATE["zone_s"][zone_of(det.stress)] += dt
                if tick - last_hist >= 2:          # chart point every 2 s
                    last_hist = tick
                    STATE["history"].append([round(tick, 1),
                                             round(det.stress, 1)])
                    del STATE["history"][:-300]    # keep last 10 minutes
            if want_tip:
                request_tip(want_tip)

            # 6. Pick the target colour
            if det.calibrating:
                target, fade = (40, 40, 40), 0.3
            elif break_alert:
                pulse = 0.6 + 0.4 * math.sin(tick * 2)
                target, fade = [c * pulse for c in BREAK_COLOR], 0.1
            elif breathe_t0:
                b = breathe_brightness(tick - breathe_t0)
                if b is None:
                    breathe_t0 = None
                    det.stress *= 0.5          # breathing helped
                    target, fade = (0, 0, 0), 0.5
                else:
                    target, fade = [c * b for c in BREATHE_COLOR], 0.05
            elif tick < flash_until:
                target, fade = (255, 255, 255), 0.0
            else:
                target = palette_color(det.cfg["palette"], det.stress)
                fade = det.cfg["fade_s"]

            # 7. Smooth fade toward the target and show it
            k = 1.0 if fade <= 0 else min(1.0, dt / fade)
            color = [c + (t - c) * k for c, t in zip(color, target)]
            leds.set(color)

            with STATE_LOCK:
                STATE.update(stress=round(det.stress, 1),
                             level_db=round(det.level, 1),
                             floor_db=det.floor and round(det.floor, 1),
                             color=[int(c) for c in color],
                             zone=zone_of(det.stress),
                             calibrating=det.calibrating,
                             breathing=bool(breathe_t0),
                             break_alert=break_alert)
            status = ("calibrating..." if det.calibrating else
                      "level %5.1f dB  floor %5.1f  stress %3d  reds %d  %s%s" %
                      (det.level, det.floor, det.stress, STATE["reds"],
                       "BREATHING " if breathe_t0 else "",
                       "BREAK! " if break_alert else ""))
            if use_gpio:
                if tick - last_log >= 0.5 and not args.quiet:
                    sys.stdout.write("\r" + status + "\x1b[K")
                    sys.stdout.flush()
            else:
                leds.draw_terminal(status)

            # 8. Log once a second (and on every event)
            if tick - last_log >= 1.0 or log_events:
                last_log = tick
                log.write("%s,%.1f,%s,%.1f,%s,%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), det.level,
                    "%.1f" % det.floor if det.floor else "",
                    det.stress, STATE["mode"], "+".join(log_events)))
                log.flush()
                log_events = []

            time.sleep(max(0.0, dt - (time.time() - tick)))
    except KeyboardInterrupt:
        pass
    finally:
        print()
        leds.off()
        log.close()


# =============================================================================
# Test helpers
# =============================================================================


def test_mic(args):
    frames = queue.Queue()
    src = AudioSource(frames, chunk_mode=args.chunk, cmd=args.audio_cmd)
    src.start()
    print("Talk, clap, bang the table. Ctrl+C to stop.\n")
    n = 0
    try:
        while True:
            if src.error:
                raise SystemExit("\nAudio problem: %s" % src.error)
            try:
                rms, peak = frames.get(timeout=3)
            except queue.Empty:
                print("No audio after 3 s - is the mic plugged in? "
                      "Try --chunk or --audio-cmd.")
                continue
            n += 1
            if n % 5 == 0:                   # print 10x per second
                bar = "#" * int(max(0, rms + 70))
                sys.stdout.write("\r%6.1f dB  peak %6.1f  |%-70s|" %
                                 (rms, peak, bar[:70]))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print()


def test_leds(args):
    leds = Leds(HAVE_GPIO and not args.sim)
    seq = [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)),
           ("BLUE", (0, 0, 255)), ("WHITE", (255, 255, 255)),
           ("OFF", (0, 0, 0))]
    try:
        for name, c in seq:
            print("Should be %s" % name)
            leds.set(c)
            time.sleep(1.5)
        print("Fading through the gamer palette...")
        for i in range(0, 101, 2):
            leds.set(palette_color(MODES["gamer"]["palette"], i))
            time.sleep(0.05)
    finally:
        leds.off()


def main():
    ap = argparse.ArgumentParser(description="Calm Cube")
    ap.add_argument("--mode", choices=list(MODES), default="kid")
    ap.add_argument("--sim", action="store_true",
                    help="no mic or LEDs; type commands instead")
    ap.add_argument("--chunk", action="store_true",
                    help="record 1 s files instead of streaming")
    ap.add_argument("--audio-cmd", help="override the recording command")
    ap.add_argument("--test-mic", action="store_true")
    ap.add_argument("--test-leds", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="don't print status (when running in background)")
    args = ap.parse_args()
    if args.test_mic:
        test_mic(args)
    elif args.test_leds:
        test_leds(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
