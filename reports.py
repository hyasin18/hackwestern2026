"""
Downloadable reports for CoCube, made from the cube's own log
(calm_cube_log.csv). Owner: Eliza. Standard library only.

  GET /report/summary.csv   -> one row per day
  GET /report/events.csv    -> one row per thing that happened
  GET /report/history?range=60|today -> chart points for the dashboard

Both open in Excel, Numbers or Google Sheets.
"""
import csv
import io
import json
import os
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_WORDS = {
    "red": "Got too loud",
    "yell": "Loud stretch",
    "bang": "Sudden noise",
    "breathe": "Breathing exercise (button)",
    "auto_breathe": "Breathing exercise (started on its own)",
    "break": "Asked for a break",
    "ack": "An adult answered",
}
MODES = {"kid": "Gentle", "focus": "Focus"}


def _zone(stress):
    return ("Calm" if stress < 35 else "Getting louder" if stress < 65
            else "Loud" if stress < 85 else "Too loud")


def _log_path():
    for p in (os.path.abspath("calm_cube_log.csv"), os.path.join(HERE, "calm_cube_log.csv")):
        if os.path.exists(p):
            return p
    return None


def _rows():
    path = _log_path()
    if not path:
        return
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                yield r["time"], float(r["stress"]), r.get("mode", ""), r.get("event", "") or ""
            except (KeyError, ValueError):
                continue


def events_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Time", "What happened", "Mode"])
    for t, stress, mode, ev in _rows():
        for e in filter(None, ev.split("+")):
            if e in EVENT_WORDS:
                d, _, hms = t.partition(" ")
                w.writerow([d, hms, EVENT_WORDS[e], MODES.get(mode, mode)])
    return out.getvalue()


def summary_csv():
    days = defaultdict(lambda: {"secs": set(), "calm": 0, "n": 0, "red": 0, "yell": 0, "bang": 0,
                                "breathe": 0, "break": 0, "hours": defaultdict(int)})
    for t, stress, mode, ev in _rows():
        d, _, hms = t.partition(" ")
        day = days[d]
        if hms not in day["secs"]:
            day["secs"].add(hms)
            day["n"] += 1
            day["calm"] += stress < 35
        for e in filter(None, ev.split("+")):
            if e in ("breathe", "auto_breathe"):
                day["breathe"] += 1
            elif e in day:
                day[e] += 1
            if e == "red":
                day["hours"][hms[:2]] += 1
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Hours monitored", "Time calm (%)", "Times it got too loud",
                "Loud stretches", "Sudden noises", "Breathing exercises",
                "Times asked for a break", "Hour it got too loud most"])
    for d in sorted(days):
        x = days[d]
        busiest = ""
        if x["hours"]:
            h = int(max(x["hours"], key=x["hours"].get))
            busiest = "%d %s" % (h % 12 or 12, "am" if h < 12 else "pm")
        w.writerow([d, round(x["n"] / 3600, 1), round(100 * x["calm"] / x["n"]) if x["n"] else "",
                    x["red"], x["yell"], x["bang"], x["breathe"], x["break"], busiest])
    return out.getvalue()


def history(rng):
    """Chart points [[unix time, stress]] for the last N minutes or today,
    squeezed to about 300 points (keeping the peak of each slice)."""
    now = time.time()
    if rng == "today":
        lt = time.localtime(now)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    else:
        start = now - max(1, min(24 * 60, int(rng))) * 60
    first = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start))
    pts = []
    for t, stress, mode, ev in _rows():
        if t >= first:                       # same format, so text order = time order
            try:
                pts.append((time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S")), stress))
            except ValueError:
                pass
    if not pts:
        return []
    step = max(1.0, (now - start) / 300)
    out = {}
    for ts, v in pts:
        k = int((ts - start) // step)
        if k not in out or v > out[k][1]:
            out[k] = [ts, v]
    return [out[k] for k in sorted(out)]


def handle_get(h, path, qs):
    """Returns True if this was a report download or chart history."""
    if path == "/report/history":
        rng = qs.partition("range=")[2].split("&")[0] or "60"
        if not (rng == "today" or rng.isdigit()):
            rng = "60"
        body = json.dumps(history(rng)).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
        return True
    makers = {"/report/summary.csv": ("daily-summary", summary_csv),
              "/report/events.csv": ("all-events", events_csv)}
    if path not in makers:
        return False
    name, make = makers[path]
    body = ("﻿" + make()).encode("utf-8")      # the BOM makes Excel read it right
    h.send_response(200)
    h.send_header("Content-Type", "text/csv; charset=utf-8")
    h.send_header("Content-Disposition",
                  'attachment; filename="cocube-%s-%s.csv"' % (name, time.strftime("%Y-%m-%d")))
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)
    return True
