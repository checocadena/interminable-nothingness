#!/usr/bin/env python3
"""
Mix a recorded session's audio (duet.py --record DIR) into one stereo WAV,
each line placed at the moment it was spoken.

  python3 tools/mixdown.py recordings/session1 out.wav [--start S] [--dur D]

S and D are seconds from the start of the session.
"""
import argparse
import glob
import json
import os
import sys
import wave
from array import array

RATE = 22050


def events(session):
    out = []
    for path in glob.glob(os.path.join(session, "timeline_*.jsonl")):
        with open(path) as f:
            out += [json.loads(line) for line in f if line.strip()]
    return out


def session_start(evs):
    return min(e.get("start", e["t"]) for e in evs)


def read_stereo(path):
    with wave.open(path) as w:
        assert w.getsampwidth() == 2 and w.getframerate() == RATE, path
        a = array("h", w.readframes(w.getnframes()))
        if sys.byteorder == "big":
            a.byteswap()
        if w.getnchannels() == 1:
            st = array("h", bytes(len(a) * 4))
            st[0::2] = a
            st[1::2] = a
            return st
        return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("out")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--dur", type=float, default=None)
    args = ap.parse_args()

    evs = events(args.session)
    t0 = session_start(evs) + args.start
    speaks = [e for e in evs if e["type"] == "speak" and e.get("audio")]
    end = max(e["start"] + e["dur"] for e in speaks)
    dur = args.dur if args.dur is not None else end - t0
    mix = array("i", bytes(int(dur * RATE) * 2 * 4))
    for e in speaks:
        if e["start"] + e["dur"] < t0 or e["start"] > t0 + dur:
            continue
        clip = read_stereo(os.path.join(args.session, e["audio"]))
        at = int((e["start"] - t0) * RATE) * 2
        for k, v in enumerate(clip):
            i = at + k
            if 0 <= i < len(mix):
                mix[i] += v
    out = array("h", (max(-32768, min(32767, v)) for v in mix))
    if sys.byteorder == "big":
        out.byteswap()
    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(out.tobytes())
    print(f"mixed {dur:.1f}s -> {args.out}")


if __name__ == "__main__":
    main()
