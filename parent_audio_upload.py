"""
Safe upload of the parent's RED recording.

calm_cube.py sends every  POST /voice  here FIRST (before Eliza's
voice_messages.py), so the dashboard's existing "Upload recording" button
keeps working, but every upload is checked properly:

  - Only devices on the local network may upload (UPLOAD_LAN_ONLY).
  - Optional PIN (UPLOAD_PIN), for busy shared Wi-Fi such as a hackathon.
  - Size limit 8 MB (the same as voice_messages.py and the dashboard).
  - The file's real type is read from its first bytes ("magic bytes"),
    not trusted from its name or the browser. Programs, scripts, zips,
    images... are all refused.
  - Only formats this cube can actually play are accepted. By default that
    is WAV only, because WAV is the format the QNX quick start image
    documents. (PARENT_UPLOAD_FORMATS can add m4a/aac after you test them.)
  - WAV files are opened and checked (not damaged, 0.3 - 60 seconds).
  - The file name sent by the phone is IGNORED. The cube always saves it as
    audio/parent_voice.<ext>, so "../../" tricks can't write anywhere else.
  - Saved without execute permission, written atomically (no half files).

It saves in exactly the place and format voice_messages.py uses
(audio/parent_voice.<ext> + audio/parent_voice.json), so the dashboard's
Listen / Replace / Remove buttons and voice_messages.parent_voice_path()
keep working unchanged.

Also serves  GET /voice_upload  - a separate upload page (voice_upload.html)
that converts ANY phone recording (m4a, mp3, ...) to WAV inside the phone's
browser before sending it, so iPhone Voice Memos work too.

Standard library only.
"""
import hmac
import ipaddress
import json
import os
import socket

import audio_manager as am
import voice_config

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import voice_messages                           # Eliza's file - read only
    FOLDER = voice_messages.FOLDER
    BASENAME = voice_messages.NAMES["red"]
    MAX_BYTES = voice_messages.MAX_BYTES
except Exception:
    FOLDER = os.path.join(HERE, "audio")
    BASENAME = "parent_voice"
    MAX_BYTES = 8 * 1024 * 1024
PAGE = os.path.join(HERE, "voice_upload.html")
MIME = {"wav": "audio/wav", "m4a": "audio/mp4", "mp3": "audio/mpeg",
        "aac": "audio/aac", "ogg": "audio/ogg", "webm": "audio/webm"}
MIN_SECONDS = 0.3


# =============================================================================
# Who may upload
# =============================================================================

def client_allowed(h, cfg):
    """True for devices on the local network (or when the check is turned off)."""
    if not cfg.bool("UPLOAD_LAN_ONLY"):
        return True
    try:
        ip = ipaddress.ip_address(h.client_address[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except (ValueError, IndexError, TypeError):
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    # Some campus networks hand out public addresses. Then allow phones in the
    # same /16 as the cube itself (they are on the same network as the cube).
    try:
        mine = ipaddress.ip_address(h.connection.getsockname()[0])
        if mine.version == 6 and mine.ipv4_mapped:
            mine = mine.ipv4_mapped
        if ip.version == 4 and mine.version == 4:
            return ipaddress.ip_network("%s/16" % mine, strict=False) == \
                ipaddress.ip_network("%s/16" % ip, strict=False)
    except (OSError, ValueError, AttributeError, socket.error):
        pass
    return False


def _pin_ok(h, qs, cfg):
    pin = cfg.str("UPLOAD_PIN")
    if not pin:
        return True
    given = h.headers.get("X-Upload-Pin", "") or \
        dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("pin", "")
    return hmac.compare_digest(given.strip().encode(), pin.encode())


# =============================================================================
# What was uploaded
# =============================================================================

def sniff(data):
    """The real file type from its first bytes, or None if it isn't audio."""
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":                        # iPhone Voice Memos, most Androids
        return "m4a"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:  # ADTS AAC
        return "aac"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:  # MPEG audio frame
        return "mp3"
    return None


def _reply(h, code, obj):
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)
    if code >= 400:
        h.close_connection = True


def _write_atomic(path, data, text=False):
    tmp = path + ".tmp"
    with open(tmp, "w" if text else "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)                            # readable, never executable
    os.replace(tmp, path)


def save_recording(data, cfg):
    """Checks and saves an uploaded recording.
    Returns (http_code, reply_dict). Used by handle_post and by the tests."""
    fmt = sniff(data)
    allowed = cfg.list("PARENT_UPLOAD_FORMATS") or ["wav"]
    if fmt is None:
        return 415, {"error": "That file isn't an audio recording."}
    if fmt not in allowed:
        return 415, {"error": "This cube can only play %s recordings. Open "
                              "/voice_upload on the cube's address - it converts "
                              "your recording automatically." % ", ".join(a.upper() for a in allowed)}
    os.makedirs(FOLDER, exist_ok=True)
    tmp = os.path.join(FOLDER, BASENAME + ".upload.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    seconds = None
    if fmt == "wav":
        try:
            info = am.wav_info(tmp)
        except am.BadAudio as e:
            os.remove(tmp)
            return 415, {"error": "The recording couldn't be read: %s." % e}
        seconds = info["seconds"]
        if seconds < MIN_SECONDS:
            os.remove(tmp)
            return 400, {"error": "That recording is too short."}
        if seconds > cfg.float("PARENT_MAX_SECONDS") + 0.5:
            os.remove(tmp)
            return 413, {"error": "Keep the recording under %d seconds." %
                                  cfg.int("PARENT_MAX_SECONDS")}
    final = os.path.join(FOLDER, BASENAME + "." + fmt)
    os.chmod(tmp, 0o644)
    os.replace(tmp, final)
    for ext in set(MIME) | {"audio", "caf", "3gp"}:   # remove an older recording
        old = os.path.join(FOLDER, BASENAME + "." + ext)
        if ext != fmt and os.path.isfile(old):
            os.remove(old)
    _write_atomic(os.path.join(FOLDER, BASENAME + ".json"),
                  json.dumps({"file": os.path.basename(final), "type": MIME[fmt]}), text=True)
    print("\n[upload] new parent recording saved: %s%s" %
          (os.path.basename(final), " (%.1f s)" % seconds if seconds else ""))
    out = {"ok": True, "file": os.path.basename(final)}
    if seconds:
        out["seconds"] = round(seconds, 1)
    return 200, out


# =============================================================================
# Web routes (called by calm_cube.py)
# =============================================================================

def handle_post(h, path, qs, cfg=None):
    """POST /voice?zone=red. Returns True if handled."""
    if path != "/voice":
        return False
    cfg = cfg or voice_config.load()
    if not client_allowed(h, cfg):
        _reply(h, 403, {"error": "Uploads are only allowed from the cube's local network."})
        return True
    if not _pin_ok(h, qs, cfg):
        _reply(h, 403, {"error": "Wrong or missing upload PIN."})
        return True
    zone = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("zone")
    if zone != "red":
        _reply(h, 400, {"error": "unknown zone"})
        return True
    try:
        size = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        size = -1
    if not 0 < size <= MAX_BYTES:
        _reply(h, 413, {"error": "The file must be under %d MB." % (MAX_BYTES // 1048576)})
        return True
    data = h.rfile.read(size)
    if len(data) != size:
        _reply(h, 400, {"error": "The upload was cut off. Try again."})
        return True
    code, reply = save_recording(data, cfg)
    if code != 200:
        print("\n[upload] refused a parent recording: %s" % reply.get("error"))
    _reply(h, code, reply)
    return True


def handle_get(h, path, qs, cfg=None):
    """GET /voice_upload -> the separate upload page. Returns True if handled."""
    if path != "/voice_upload":
        return False
    cfg = cfg or voice_config.load()
    if not client_allowed(h, cfg):
        _reply(h, 403, {"error": "Only devices on the cube's local network can open this."})
        return True
    try:
        with open(PAGE, "rb") as fh:
            body = fh.read()
    except OSError:
        _reply(h, 404, {"error": "voice_upload.html is missing"})
        return True
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)
    return True
