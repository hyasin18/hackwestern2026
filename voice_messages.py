"""
Parent voice message for CoCube: a parent uploads a short recording on the
dashboard, and the cube plays it through its speaker when it turns red.

Owner: Eliza. Used by calm_cube.py's web server. Standard library only.

  POST /voice?zone=red          body = the audio file  -> saves it
  GET  /voice/red               -> lets the parent listen back (Range-aware)
  GET  /voice_delete?zone=red   -> removes it

Saved as audio/parent_voice.<ext> next to this file (ext = wav, m4a, mp3...),
on the cube only. voice.py can call parent_voice_path() to find it.
"""
import json
import os
import re
from urllib.parse import unquote

ZONES = ("red",)                         # red = parent's voice; yellow = AI voice
MAX_BYTES = 8 * 1024 * 1024              # 8 MB is plenty for a short message
FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
NAMES = {"red": "parent_voice"}
EXT = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3",
       "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac", "audio/ogg": "ogg",
       "audio/webm": "webm", "video/mp4": "m4a"}


def _meta(zone):
    return os.path.join(FOLDER, NAMES[zone] + ".json")


def _audio(zone):
    """Path of the saved recording for this zone, or None."""
    try:
        with open(_meta(zone)) as fh:
            p = os.path.join(FOLDER, json.load(fh)["file"])
        return p if os.path.exists(p) else None
    except Exception:
        return None


def parent_voice_path():
    """For voice.py: the parent's recording to play on red, or None."""
    return _audio("red")


def summary():
    """{zone: {"v": version, "name": original file name} or None}"""
    out = {}
    for z in ZONES:
        audio = _audio(z)
        out[z] = {"v": int(os.path.getmtime(audio))} if audio else None
    return out


def _reply(h, code, body=b'{"ok":true}', ctype="application/json"):
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def handle_post(h, path, qs):
    """Returns True if this was a voice upload."""
    if path != "/voice":
        return False
    zone = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("zone")
    if zone not in ZONES:
        _reply(h, 400, b'{"error":"unknown zone"}')
        return True
    size = int(h.headers.get("Content-Length") or 0)
    if not 0 < size <= MAX_BYTES:
        _reply(h, 413, b'{"error":"file must be under 8 MB"}')
        return True
    data = h.rfile.read(size)
    ctype = (h.headers.get("Content-Type") or "audio/mpeg").split(";")[0].strip()
    if not (ctype.startswith("audio/") or ctype in ("video/mp4", "video/webm",
                                                     "application/octet-stream")):
        _reply(h, 415, b'{"error":"that is not an audio file"}')
        return True
    name = unquote(h.headers.get("X-File-Name", ""))
    ext = (os.path.splitext(name)[1][1:].lower() or EXT.get(ctype, "audio"))
    ext = re.sub(r"[^a-z0-9]", "", ext)[:5] or "audio"
    os.makedirs(FOLDER, exist_ok=True)
    old = _audio(zone)
    audio = os.path.join(FOLDER, NAMES[zone] + "." + ext)
    with open(audio + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(audio + ".tmp", audio)
    if old and old != audio:
        os.remove(old)
    with open(_meta(zone), "w") as fh:
        json.dump({"file": os.path.basename(audio), "type": ctype}, fh)
    _reply(h, 200)
    return True


def handle_get(h, path, qs):
    """Returns True if this was a voice request."""
    if path == "/voice_delete":
        zone = qs.partition("zone=")[2].split("&")[0]
        if zone in ZONES:
            for p in (_audio(zone), _meta(zone)):
                if p and os.path.exists(p):
                    os.remove(p)
        _reply(h, 200)
        return True
    m = re.fullmatch(r"/voice/(\w+)", path)
    if not m:
        return False
    zone = m.group(1)
    audio = _audio(zone) if zone in ZONES else None
    if not audio:
        _reply(h, 404, b'{"error":"no recording"}')
        return True
    try:
        with open(_meta(zone)) as fh:
            ctype = json.load(fh).get("type", "audio/mpeg")
    except Exception:
        ctype = "audio/mpeg"
    with open(audio, "rb") as fh:
        data = fh.read()
    total = len(data)
    rng = re.fullmatch(r"bytes=(\d*)-(\d*)", h.headers.get("Range", "").strip())
    if rng and (rng.group(1) or rng.group(2)):
        if rng.group(1):
            start = int(rng.group(1))
            end = int(rng.group(2)) if rng.group(2) else total - 1
        else:                                    # last N bytes
            start, end = max(0, total - int(rng.group(2))), total - 1
        end = min(end, total - 1)
        if start > end:
            h.send_response(416)
            h.send_header("Content-Range", "bytes */%d" % total)
            h.end_headers()
            return True
        part = data[start:end + 1]
        h.send_response(206)
        h.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
    else:
        part = data
        h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(len(part)))
    h.send_header("Cache-Control", "no-cache")
    h.end_headers()
    h.wfile.write(part)
    return True
