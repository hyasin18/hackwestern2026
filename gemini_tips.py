"""
Calming tips for the CoCube dashboard, from Google's Gemini API.
"""
import json
import os
import random
import ssl
import time
import urllib.error
import urllib.request

GEMINI_MODELS = ["gemini-3.1-flash-lite", "gemini-flash-latest", "gemini-3.8-flash",
                 "gemini-2.5-flash-lite"]
PER_MODEL_TIMEOUT_S = 8   # give up on a model after this many seconds
API = "https://generativelanguage.googleapis.com/v1beta/models"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "gemini_key.txt")

# What Gemini is told about its job
SYSTEM_PROMPT = """You write ONE calming tip for someone sitting at a desk
next to the CoCube, a light that changes colour when the room gets too loud.
The person may be a child (including autistic children), a teen, or an adult
studying or working.

Rules:
- One or two short sentences, at most 25 words. Simple words a 9-year-old knows.
- Kind and calm. Never say "calm down", never blame, never call behaviour bad.
- Give one concrete thing to do right now. Pick from many kinds: a drink of
  water, headphones or ear defenders, moving somewhere quieter, pressing palms
  together, squeezing something soft, counting things you can see, stretching,
  a short walk, asking a grown-up or friend for help, slow breathing.
- Only sometimes mention the cube or its light; most tips should not.
- Never repeat or closely copy a tip listed under "Recent tips".
- No medical advice, no diagnoses, no mention of autism.
- In focus mode the person is studying or working; keep it brief and practical.
- Reply with the tip only. No emoji, no quotes, no lists."""

OFFLINE_TIPS = [
    "Press the cube's button and breathe with the light: in while it gets "
    "brighter, out while it fades.",
    "Push your palms together hard for five seconds, then let go. Do it "
    "three times.",
    "Try headphones or move to a quieter spot for a few minutes.",
    "Watch the light and count slowly to five each time it gets brighter.",
    "Have a sip of cold water and look at one thing far away.",
    "Name five things you can see and four things you can touch.",
    "Roll your shoulders up to your ears, hold, and drop them. Twice more.",
    "Sit quietly with the cube and see if you can help the light turn back to green.",
    "It's okay to take a break. Hold the button to let someone know.",
]

_CA_FILES = ["/etc/ssl/certs/cacert.pem", "/etc/ssl/cert.pem"]
_good_model = None   # last model that answered


def _ssl_context():
    for f in _CA_FILES:              # QNX keeps its certificates here
        if os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


def _api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as fh:
            key = fh.read().strip()
    return key


def _request(url, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else
                                 "GET", headers={"x-goog-api-key": key,
                                                 "Content-Type":
                                                 "application/json"})
    with urllib.request.urlopen(req, timeout=PER_MODEL_TIMEOUT_S,
                                context=_ssl_context()) as r:
        return json.loads(r.read().decode())


_recent = []          # last few tips so Gemini doesn't repeat itself


def describe(ctx):
    """Turn the cube's numbers into a short message for Gemini.
    Only numbers are sent, never sound."""
    return ("Mode: %(mode)s. The cube just %(why)s. Stress meter: "
            "%(stress)d out of 100. Today: turned red %(reds)d times, "
            "%(bangs)d table bangs, %(yells)d times yelling, "
            "%(breaks)d break requests. Local time: %(clock)s. "
            "%(recent)s"
            "Write the tip." % dict(ctx, recent=("Recent tips: " + " | ".join(_recent).rstrip(".") + ". ")
                                    if _recent else ""))


def get_tip(ctx):
    """Returns (tip_text, source) where source is 'gemini' or 'offline'."""
    global _good_model
    key = _api_key()
    if not key:
        return random.choice(OFFLINE_TIPS), "offline, no API key"
    body = {"system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": describe(ctx)}]}]}
    order = ([_good_model] if _good_model else []) + \
        [m for m in GEMINI_MODELS if m != _good_model]
    for model in order:
        try:
            out = _request("%s/%s:generateContent" % (API, model), key, body)
            parts = out["candidates"][0]["content"]["parts"]
            text = " ".join(p.get("text", "") for p in parts).strip()
            if text:
                if model != _good_model:
                    print("\nGemini: using %s" % model)
                _good_model = model
                _recent.append(text[:300])
                del _recent[:-4]
                return text[:300], "gemini"
            print("\nGemini %s: empty reply, trying next" % model)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):     # key problem: other models won't help
                print("\nGemini key error %s: %s" % (e.code, e.read()[:200]))
                break
            print("\nGemini %s: error %s, trying next" % (model, e.code))
        except Exception as e:                # timeout, no internet, bad reply
            print("\nGemini %s: %s, trying next" % (model, e))
    return random.choice(OFFLINE_TIPS), "offline"


if __name__ == "__main__":
    # Quick test:  python3 gemini_tips.py
    tip, src = get_tip(dict(mode="kid", why="turned red", stress=88, reds=2,
                            bangs=3, yells=1, breaks=0,
                            clock=time.strftime("%H:%M")))
    print("[%s] %s" % (src, tip))
