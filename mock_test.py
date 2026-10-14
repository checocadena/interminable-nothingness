#!/usr/bin/env python3
"""
Protocol test for duet.py: runs both roles on localhost with --mock, kills and
restarts each node once, then checks from the speech traces that the two voices
never overlap and that turns alternate.

  python3 mock_test.py            # ~50 s
"""
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORTS = {"light": 8101, "dark": 8102}
CLEAN_RUN, AFTER_RESTART = 20, 12


def start(role, log_dir):
    peer = "dark" if role == "light" else "light"
    cmd = [sys.executable, os.path.join(HERE, "duet.py"), "--role", role,
           "--port", str(PORTS[role]), "--peer", f"http://127.0.0.1:{PORTS[peer]}",
           "--mock", "--trace", "--log-dir", log_dir,
           "--wait-timeout", "5", "--reply-timeout", "8", "--nudge-every", "3"]
    if role == "light":
        cmd.append("--start")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def read_traces(log_dir):
    events = []
    for role in PORTS:
        with open(os.path.join(log_dir, f"duet_{role}.log")) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 7 and parts[2] == "TRACE":
                    _, _, _, ev, r, seq, t = parts
                    events.append((float(t), ev, r, int(seq)))
    return sorted(events)


def intervals(events, kills):
    """Turn START/END events into (start, end, role) speech intervals."""
    open_, out = {}, []
    for t, ev, role, _ in events:
        if ev == "START":
            if role in open_:  # previous line was cut off by a kill
                out.append((open_[role], next(k for r, k in kills if r == role and k > open_[role]), role, True))
            open_[role] = t
        elif role in open_:
            out.append((open_.pop(role), t, role, False))
    for role, s in open_.items():
        out.append((s, time.time(), role, True))
    return sorted(out)


def main():
    log_dir = tempfile.mkdtemp(prefix="duet_mock_")
    procs, kills, marks = {}, [], {}
    try:
        t0 = time.time()
        procs["dark"] = start("dark", log_dir)   # start in "wrong" order on purpose
        time.sleep(1)
        procs["light"] = start("light", log_dir)
        time.sleep(CLEAN_RUN)

        for role in ("dark", "light"):
            print(f"... killing {role} (SIGKILL) at +{time.time() - t0:.1f}s, restarting in 3s")
            procs[role].send_signal(signal.SIGKILL)
            procs[role].wait()
            kills.append((role, time.time()))
            time.sleep(3)
            procs[role] = start(role, log_dir)
            marks[role] = time.time()
            time.sleep(AFTER_RESTART)
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            p.wait()
    end = time.time()
    kills.append(("light", end))
    kills.append(("dark", end))

    spans = intervals(read_traces(log_dir), kills)
    first_kill = kills[0][1]

    overlaps = [(a, b) for a, b in zip(spans, spans[1:]) if b[0] < a[1] - 1e-4]
    clean = [s for s in spans if s[1] <= first_kill]
    clean_breaks = [i for i in range(1, len(clean)) if clean[i][2] == clean[i - 1][2]]
    all_breaks = [(spans[i - 1], spans[i]) for i in range(1, len(spans)) if spans[i][2] == spans[i - 1][2]]

    print(f"\nlogs: {log_dir}\n")
    print("Conversation (first 12 lines):")
    with open(os.path.join(log_dir, "duet_light.log")) as fl, open(os.path.join(log_dir, "duet_dark.log")) as fd:
        lines = sorted(l.rstrip() for l in fl.readlines() + fd.readlines() if "TRACE" not in l)
    for l in lines[:14]:
        print("  " + l)
    print("\nRecovery events:")
    for l in lines:
        if "] " in l and ("restart" in l or "reopen" in l or "moved on" in l or "never sent" in l
                          or "dropping" in l or "listening" in l):
            print("  " + l)

    print("\nSpeech timeline around each kill:")
    for role, k in kills[:2]:
        print(f"  -- {role} killed at +{k - t0:.2f}s")
        near = [s for s in spans if k - 4 < s[0] < k + 14]
        for s, e, r, cut in near:
            print(f"     +{s - t0:6.2f}s .. +{e - t0:6.2f}s  {r.upper():5}{'  (cut off by kill)' if cut else ''}")

    print("\nResults:")
    print(f"  utterances total:               {len(spans)}")
    print(f"  utterances in clean run:        {len(clean)}")
    print(f"  overlapping utterances:         {len(overlaps)}")
    print(f"  alternation breaks, clean run:  {len(clean_breaks)}")
    print(f"  alternation breaks, overall:    {len(all_breaks)}")
    for a, b in all_breaks:
        print(f"     {a[2].upper()} spoke twice in a row at +{a[0] - t0:.2f}s / +{b[0] - t0:.2f}s"
              f"{' (previous line cut off by kill)' if a[3] else ''}")
    resumed = {r: sum(1 for s in spans if s[0] > marks[r]) for r in marks}
    print(f"  utterances after dark restart:  {resumed['dark']}")
    print(f"  utterances after light restart: {resumed['light']}")
    ok = not overlaps and not clean_breaks and len(clean) > 10 and all(v > 4 for v in resumed.values())
    print("\nPASS" if ok else "\nFAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
