"""
Plan B speaker for the CoCube: run this on the LAPTOP.

The cube (Raspberry Pi, QNX) sends each sound it wants to say over Wi-Fi;
this plays it on the laptop's sound output - plug or pair the speaker that
sits inside the cube to the laptop (Bluetooth or AUX) and it sounds like
the cube is talking.

  python laptop_speaker.py

Then on the Pi:
  python3 calm_cube.py --net --speaker-laptop <this laptop's IP>

Needs: python -m pip install sounddevice numpy   (same as laptop_mic.py)
If Windows asks about the firewall, click "Allow access" (tick Public too).
"""
import io
import socket
import struct
import threading
import wave

import numpy as np
import sounddevice as sd

PORT = 9998


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            raise ConnectionError("cube hung up")
        buf += part
    return buf


def handle(conn, addr):
    try:
        size = struct.unpack("!I", recv_exact(conn, 4))[0]
        data = recv_exact(conn, size)
        with wave.open(io.BytesIO(data)) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width == 2:
            audio = np.frombuffer(raw, dtype="<i2")
        elif width == 1:
            audio = ((np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8)
        elif width == 4:
            audio = (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
        else:
            raise ValueError("unsupported WAV sample width %d" % width)
        audio = audio.reshape(-1, ch)
        print("Playing %.1f s from the cube" % (len(audio) / float(rate)))
        sd.play(audio, rate)
        conn.settimeout(0.05)
        stream = sd.get_stream()
        while stream.active:
            try:
                if conn.recv(16) == b"":          # cube asked us to stop
                    sd.stop()
                    print("  (stopped by the cube)")
                    return
            except socket.timeout:
                pass
        conn.sendall(b"done")
    except Exception as e:
        print("Problem: %s" % e)
        try:
            conn.sendall(("err %s" % e).encode())
        except Exception:
            pass
    finally:
        conn.close()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(4)
    host = socket.gethostbyname(socket.gethostname())
    print("Laptop speaker ready on port %d. This laptop's IP is probably %s" % (PORT, host))
    print("(check with ipconfig - use the Wi-Fi adapter's IPv4 address)")
    print("On the Pi: python3 calm_cube.py --net --speaker-laptop <that IP>")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
