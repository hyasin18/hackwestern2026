#!/usr/bin/env python3
"""
ONE command to run the whole CoCube from the laptop:

    python start_cube.py

It does three jobs in this one window:
  1. laptop mic  -> sends loudness numbers to the Pi   (like laptop_mic.py)
  2. laptop speaker <- plays the cube's voice            (like laptop_speaker.py)
  3. logs in to the Pi and starts calm_cube.py there   (asks for the password: qnxuser)

Options:  python start_cube.py --mode focus
          python start_cube.py --pi 172.23.42.90 --device 3   (pick a mic, see --list)
Ctrl+C stops everything.
Needs: python -m pip install sounddevice numpy
"""
import argparse
import io
import math
import socket
import struct
import subprocess
import sys
import threading
import wave

import numpy as np
import sounddevice as sd

MIC_PORT, SPEAKER_PORT = 9999, 9998
RATE, FRAME = 16000, 320


# ---------------- speaker: play what the cube sends ----------------
def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            raise ConnectionError("cube hung up")
        buf += part
    return buf


def _play(conn):
    try:
        size = struct.unpack("!I", _recv_exact(conn, 4))[0]
        with wave.open(io.BytesIO(_recv_exact(conn, size))) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width == 2:
            audio = np.frombuffer(raw, dtype="<i2")
        elif width == 1:
            audio = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8
        else:
            audio = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
        out = sd.OutputStream(samplerate=rate, channels=ch, dtype="int16")
        audio = audio.reshape(-1, ch)
        conn.setblocking(False)
        with out:
            for i in range(0, len(audio), 2048):       # small pieces so we can stop
                out.write(np.ascontiguousarray(audio[i:i + 2048]))
                try:
                    if conn.recv(16) == b"":             # the cube said stop
                        return
                except (BlockingIOError, InterruptedError):
                    pass
        conn.setblocking(True)
        conn.sendall(b"done")
    except Exception as e:
        try:
            conn.sendall(("err %s" % e).encode())
        except Exception:
            pass
    finally:
        conn.close()


def speaker_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", SPEAKER_PORT))
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_play, args=(conn,), daemon=True).start()


# ---------------- mic: send loudness numbers to the cube ----------------
def to_db(v):
    return 20 * math.log10(max(float(v), 1.0) / 32768.0)


def start_mic(pi, device):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def callback(indata, frames, t, status):
        x = indata[:, 0].astype(np.float32)
        parts = []
        for i in range(0, len(x) - FRAME + 1, FRAME):
            f = x[i:i + FRAME]
            parts.append("%.1f,%.1f" % (to_db(np.sqrt(np.mean(f * f))),
                                        to_db(np.max(np.abs(f)))))
        if parts:
            try:
                sock.sendto(";".join(parts).encode(), (pi, MIC_PORT))
            except OSError:
                pass

    stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                            blocksize=FRAME, device=device, callback=callback)
    stream.start()
    return stream


def my_ip_towards(pi):
    """The laptop's address on the same network as the Pi (not VirtualBox etc.)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((pi, MIC_PORT))
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Run the whole CoCube from the laptop")
    ap.add_argument("--pi", default="172.23.42.90", help="the Pi's IP address")
    ap.add_argument("--mode", default="kid", choices=["kid", "focus"])
    ap.add_argument("--device", type=int, help="microphone number (see --list)")
    ap.add_argument("--list", action="store_true", help="list microphones")
    ap.add_argument("--folder", default="~/hackwestern2026", help="folder on the Pi")
    args = ap.parse_args()
    if args.list:
        print(sd.query_devices())
        return

    me = my_ip_towards(args.pi)
    threading.Thread(target=speaker_server, daemon=True).start()
    mic = start_mic(args.pi, args.device)
    print("Laptop %s: mic -> Pi %s, cube voice -> this laptop's speaker." % (me, args.pi))
    print("Logging in to the Pi (password: qnxuser) ... Ctrl+C stops everything.\n")
    cmd = ("cd %s && python3 calm_cube.py --net --mode %s --speaker-laptop %s"
           % (args.folder, args.mode, me))
    try:
        subprocess.call(["ssh", "-t", "-o", "MACs=hmac-sha2-256",
                         "-o", "StrictHostKeyChecking=accept-new",
                         "qnxuser@" + args.pi, cmd])
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
