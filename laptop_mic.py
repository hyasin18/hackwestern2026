#!/usr/bin/env python3
"""
Plan B for the Calm Cube: use the LAPTOP's microphone (or a USB mic plugged
into the laptop) and send how-loud-it-is numbers to the Pi over Wi-Fi.
Only loudness numbers are sent, never sound.

Setup (once):   python -m pip install sounddevice numpy
Run:            python laptop_mic.py 172.23.42.90
List mics:      python laptop_mic.py --list
Pick a mic:     python laptop_mic.py 172.23.42.90 --device 3

On the Pi, run:  python3 calm_cube.py --net
"""
import argparse
import math
import socket
import sys
import time

import numpy as np
import sounddevice as sd

RATE = 16000
FRAME = 320          # 20 ms, same as calm_cube.py
PORT = 9999


def to_db(v):
    return 20 * math.log10(max(float(v), 1.0) / 32768.0)


def main():
    ap = argparse.ArgumentParser(description="Send mic loudness to the Pi")
    ap.add_argument("pi_ip", nargs="?", help="the Pi's address, e.g. 172.23.42.90")
    ap.add_argument("--device", type=int, help="microphone number from --list")
    ap.add_argument("--list", action="store_true", help="list microphones")
    args = ap.parse_args()

    if args.list or not args.pi_ip:
        print(sd.query_devices())
        if not args.pi_ip:
            print("\nUsage: python laptop_mic.py <pi-ip-address> [--device N]")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.pi_ip, PORT)
    latest = {"rms": -96.0, "sent": 0}

    def callback(indata, frames, time_info, status):
        x = indata[:, 0].astype(np.float32)
        parts = []
        for i in range(0, len(x) - FRAME + 1, FRAME):
            f = x[i:i + FRAME]
            rms = to_db(np.sqrt(np.mean(f * f)))
            peak = to_db(np.max(np.abs(f)))
            parts.append("%.1f,%.1f" % (rms, peak))
            latest["rms"] = rms
        if parts:
            try:
                sock.sendto(";".join(parts).encode(), target)
                latest["sent"] += len(parts)
            except OSError as e:
                print("\nSend failed: %s" % e)

    print("Sending mic levels to %s:%d. Clap to test. Ctrl+C to stop.\n"
          % target)
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                        blocksize=FRAME, device=args.device,
                        callback=callback):
        try:
            while True:
                time.sleep(0.1)
                bar = "#" * int(max(0, latest["rms"] + 70))
                sys.stdout.write("\r%6.1f dB |%-60s| sent %d"
                                 % (latest["rms"], bar[:60], latest["sent"]))
                sys.stdout.flush()
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
