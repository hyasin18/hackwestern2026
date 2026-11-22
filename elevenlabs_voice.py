"""
ElevenLabs text-to-speech for CoCube's YELLOW messages.

  text from Gemini  ->  ElevenLabs API  ->  WAV file in generated_audio/

What is sent to ElevenLabs: ONLY the text of the calming suggestion.
Never microphone audio, never room sound, never numbers about the child.

  - Official REST API: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
  - Model: eleven_flash_v2_5 (ElevenLabs' low-latency model) by default
  - Asks for raw 16-bit PCM (pcm_22050) and wraps it in a WAV header here,
    because WAV is the format the QNX quick start image documents for playback.
  - The same text with the same voice is generated once, then reused from
    generated_audio/ (saves credits and works offline next time).
  - The API key comes from the ELEVENLABS_API_KEY environment variable or
    config.local. It is never printed, logged or sent to the dashboard.

Quick test (generates a file, does NOT play it):
    python3 elevenlabs_voice.py "It's getting a little loud. Let's take a slow breath."

Standard library only.
"""
import hashlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

import audio_manager
import voice_config

API = "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=pcm_%d"
CACHE_DIR = audio_manager.CACHE_DIR
MAX_CACHED = 60                      # keep at most this many generated files
MAX_CHARS = 300                      # tips are short; never send long text
_CA_FILES = ["/etc/ssl/certs/cacert.pem", "/etc/ssl/cert.pem"]   # QNX keeps certs here


class TTSError(Exception):
    """kind is one of: no_key, bad_key, quota, voice, bad_request, rate_limit,
    server, network, timeout, bad_audio, paused"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


_paused_until = 0.0                   # after errors, don't hammer the API
_last_error = None


def _ssl_context():
    for f in _CA_FILES:
        if os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


def _cache_path(text, cfg):
    key = "|".join([cfg.str("ELEVENLABS_VOICE_ID"), cfg.str("ELEVENLABS_MODEL_ID"),
                    cfg.str("ELEVENLABS_SAMPLE_RATE"), cfg.str("ELEVENLABS_SPEED"), text])
    return os.path.join(CACHE_DIR, "tts_%s.wav" % hashlib.sha1(key.encode()).hexdigest()[:16])


def _tidy_cache():
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
                 if f.startswith("tts_") and f.endswith(".wav")]
        files.sort(key=os.path.getmtime)
        for f in files[:-MAX_CACHED]:
            os.remove(f)
    except OSError:
        pass


def _error_detail(e):
    """ElevenLabs puts the reason in {"detail": {...}} - pull out the words."""
    try:
        body = json.loads(e.read().decode(errors="replace") or "{}")
        d = body.get("detail", body)
        if isinstance(d, dict):
            return (d.get("status") or d.get("code") or d.get("type") or "",
                    d.get("message") or "")
        return "", str(d)[:200]
    except Exception:
        return "", ""


def is_configured(cfg=None):
    cfg = cfg or voice_config.load()
    return bool(cfg.str("ELEVENLABS_API_KEY"))


def last_error():
    return _last_error


def synthesize(text, cfg=None):
    """Returns (wav_path, from_cache). Raises TTSError on any problem."""
    global _paused_until, _last_error
    cfg = cfg or voice_config.load()
    text = " ".join((text or "").split())[:MAX_CHARS]
    if not text:
        raise TTSError("bad_request", "no text to speak")
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(text, cfg)
    if os.path.exists(path):
        try:
            audio_manager.wav_info(path)
            os.utime(path, None)                     # keep it in the cache longer
            return path, True
        except audio_manager.BadAudio:
            os.remove(path)                          # damaged - make it again

    key = cfg.str("ELEVENLABS_API_KEY")
    if not key:
        raise TTSError("no_key", "no ElevenLabs API key (set ELEVENLABS_API_KEY in config.local)")
    if time.time() < _paused_until:
        raise TTSError("paused", "ElevenLabs paused for %d s after an earlier error (%s)"
                       % (_paused_until - time.time(), _last_error))

    rate = cfg.int("ELEVENLABS_SAMPLE_RATE") or 22050
    body = {"text": text,
            "model_id": cfg.str("ELEVENLABS_MODEL_ID"),
            "voice_settings": {"stability": 0.6, "similarity_boost": 0.75,
                               "speed": max(0.7, min(1.2, cfg.float("ELEVENLABS_SPEED")))}}
    req = urllib.request.Request(
        API % (cfg.str("ELEVENLABS_VOICE_ID"), rate), method="POST",
        data=json.dumps(body).encode(),
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "Accept": "audio/*, application/octet-stream"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.float("ELEVENLABS_TIMEOUT_S") or 10,
                                    context=_ssl_context()) as r:
            ctype = r.headers.get("Content-Type", "")
            pcm = r.read()
    except urllib.error.HTTPError as e:
        status, msg = _error_detail(e)
        if e.code == 401 and "quota" in status:
            kind, pause = "quota", 600
        elif e.code in (401, 403):
            kind, pause = "bad_key", 600
        elif e.code == 404 or "voice" in status:
            kind, pause = "voice", 600
        elif e.code == 429:
            kind, pause = "rate_limit", 60
        elif e.code >= 500:
            kind, pause = "server", 60
        else:
            kind, pause = "bad_request", 30
        _paused_until = time.time() + pause
        _last_error = "HTTP %d %s %s" % (e.code, status, msg)
        raise TTSError(kind, _last_error.strip())
    except (socket.timeout, TimeoutError):
        _paused_until, _last_error = time.time() + 30, "timed out"
        raise TTSError("timeout", "ElevenLabs did not answer in time")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        kind = "timeout" if isinstance(reason, socket.timeout) else "network"
        _paused_until, _last_error = time.time() + 30, "no connection (%s)" % reason
        raise TTSError(kind, "could not reach ElevenLabs: %s" % reason)
    except OSError as e:
        _paused_until, _last_error = time.time() + 30, str(e)
        raise TTSError("network", "network error: %s" % e)

    if "json" in ctype or len(pcm) < 2000:           # an error dressed as success
        _last_error = "unexpected reply (%s, %d bytes)" % (ctype, len(pcm))
        raise TTSError("bad_audio", _last_error)
    audio_manager.write_wav_from_pcm16(pcm, path, rate)
    try:
        info = audio_manager.wav_info(path)
    except audio_manager.BadAudio as e:
        os.remove(path)
        raise TTSError("bad_audio", "ElevenLabs audio could not be saved: %s" % e)
    _last_error = None
    _tidy_cache()
    print("\n[elevenlabs] generated %.1f s of speech in %.1f s -> %s" %
          (info["seconds"], time.time() - t0, os.path.basename(path)))
    return path, False


if __name__ == "__main__":
    words = " ".join(sys.argv[1:]) or "It's getting a little loud. Let's take a slow breath together."
    try:
        p, cached = synthesize(words)
        info = audio_manager.wav_info(p)
        print("1. Audio generation:  OK%s" % (" (reused from cache, no API call)" if cached else ""))
        print("2. File saved:        OK  %s" % p)
        print("   %.1f seconds, %d Hz, %d-bit, %d channel(s)" %
              (info["seconds"], info["rate"], info["bits"], info["channels"]))
        print("3. Speaker playback:  NOT TESTED by this command.")
        print("   Run:  python3 test_voice.py yellow   to hear it on the cube.")
    except TTSError as e:
        print("Audio generation FAILED [%s]: %s" % (e.kind, e))
        sys.exit(1)
