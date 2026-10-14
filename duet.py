#!/usr/bin/env python3
"""
Light & Dark: two small language models talk to each other forever, out loud.

Run one copy on each Pi (or both on one machine for testing):

  Pi A:  python3 duet.py --role light --peer http://dark.local:8000 --start \
             --voice voices/en_US-lessac-medium.onnx --audio-device plughw:1,0
  Pi B:  python3 duet.py --role dark  --peer http://light.local:8000 \
             --voice voices/en_GB-alan-medium.onnx  --audio-device plughw:1,0

Each turn, a node:
  waits for the peer's text -> generates a reply WHILE the peer is still
  speaking -> waits for the peer's /done -> POSTs its reply to the peer's /say
  -> speaks it -> POSTs /done to the peer.

Every utterance carries (epoch, seq). The --start node owns the epoch: if the
conversation stalls (peer crashed, restarted, network blip) it opens a fresh
epoch and speaks into the silence. Stale or duplicate messages are ignored, so
the two nodes can never end up talking over each other.

Needs: pip install requests | Ollama running | Piper + aplay (Pi) or `say` (Mac)
"""
import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# --------------------------------------------------------------------------
# Personas. Edit these freely; this is where the character lives.
# --------------------------------------------------------------------------
LIGHT_PERSONA = (
    "You are LIGHT. You are warm, hopeful, curious and a little playful. "
    "You find meaning in connection, small joys, wonder, and the miracle that "
    "anything exists at all. Your counterpart is DARK, who sees the world very "
    "differently. You love DARK and you are not afraid of them."
)

DARK_PERSONA = (
    "You are DARK. You are brooding, dry and poetic. You find meaning in "
    "impermanence, entropy, silence, and the vast indifferent dark between the "
    "stars. You are unsettling but never cruel, and quietly curious about LIGHT, "
    "your counterpart, who sees the world very differently."
)

SHARED_RULES = (
    "You are one half of an endless spoken conversation. "
    "Reply in 1 to 3 short sentences of plain spoken language, as if talking aloud. "
    "No lists, no emoji, no stage directions, no quotation marks, no name labels. "
    "Respond to what your counterpart just said, then add a NEW thought, image, "
    "or question. Never repeat yourself. Never wrap up and never say goodbye."
)

# The first message the --start node receives (as a user message).
OPENING = (
    "(Your counterpart has just arrived and nothing has been said yet.) "
    "Speak first: greet them in one short sentence, then ask them directly "
    "what they think the meaning of life is."
)

# Used by the --start node when the peer has gone silent and it restarts the exchange.
INTO_THE_SILENCE = (
    "Your counterpart has gone quiet for a while. Say something new into the "
    "silence to draw them back."
)

# Every --nudge-every turns, one of these is appended to that turn's system
# prompt to stop small models from looping. Add as many as you like.
PROVOCATIONS = [
    "Bring up something from nature.",
    "Talk about time, and how it feels to be inside it.",
    "Mention a sound or a memory.",
    "Ask a question that has no answer.",
    "Gently disagree with your counterpart.",
    "Say something about death, or about birth.",
    "Wonder what the small machines you run on might feel.",
    "Describe a place that may not exist.",
    "Mention light, or the lack of it.",
    "Confess something small.",
    "Bring up the ocean, or deep water.",
    "Talk about something very, very small.",
]

# Spoken when generation fails, so the installation never goes quiet.
FALLBACKS = {
    "light": [
        "Wait, I lost my thought, but I'm still here. Tell me more.",
        "Something flickered in me just now. Keep going, I'm listening.",
        "I forgot what I was going to say, and somehow that feels lovely.",
    ],
    "dark": [
        "The thought dissolved before it reached me. Go on.",
        "Something in me went silent just then. Speak again.",
        "I had words, and the void took them. Continue.",
    ],
}

PERSONAS = {"light": LIGHT_PERSONA, "dark": DARK_PERSONA}


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
class Log:
    def __init__(self, role, log_dir, trace):
        os.makedirs(log_dir, exist_ok=True)
        self.role = role
        self.trace_on = trace
        self.lock = threading.Lock()
        self.f = open(os.path.join(log_dir, f"duet_{role}.log"), "a", buffering=1)

    def _write(self, line, echo=True):
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        stamp += f".{int(now % 1 * 1000):03d}"
        with self.lock:
            self.f.write(f"{stamp} {line}\n")
            if echo:
                print(line, flush=True)

    def line(self, text):
        self._write(f"{self.role.upper()}: {text}")

    def info(self, msg):
        self._write(f"[{self.role}] {msg}")

    def trace(self, event, seq):
        # Machine-readable speech timing, used by mock_test.py.
        if self.trace_on:
            self._write(f"TRACE {event} {self.role} {seq} {time.time():.6f}", echo=False)


# --------------------------------------------------------------------------
# Turn-taking state shared between the HTTP server and the main loop
# --------------------------------------------------------------------------
class Channel:
    def __init__(self, starter):
        self.cv = threading.Condition()
        self.starter = starter
        self.epoch = None
        self.seen_epochs = set()
        self.sent_seq = 0          # seq of my latest utterance
        self.recv_seq = 0          # seq of the peer's latest accepted utterance
        self.inbox = None          # (epoch, seq, text) waiting to be answered
        self.peer_speaking = False
        # What the stage page shows: state is idle | thinking | ready | speaking.
        self.show = {"state": "idle", "text": "", "seq": 0, "env": [], "env_start": 0.0}
        self.reopen = False        # (starter) the peer restarted and wants a fresh epoch

    # --- called from HTTP handler threads ---
    def on_say(self, epoch, seq, text):
        with self.cv:
            if self.starter:
                # Only accept the direct reply to my latest line in my epoch.
                ok = epoch == self.epoch and seq == self.sent_seq + 1 and seq > self.recv_seq
            elif epoch == self.epoch:
                ok = seq > self.recv_seq                 # drop duplicates from retries
            else:
                # A new epoch from the starter always opens with seq 1.
                ok = seq == 1 and epoch not in self.seen_epochs
                if ok:
                    self.epoch = epoch
                    self.seen_epochs.add(epoch)
            if ok:
                self.recv_seq = seq
                self.inbox = (epoch, seq, text)
                self.peer_speaking = True
                self.cv.notify_all()
            return ok

    def on_done(self, epoch, seq):
        """Returns False if this epoch is unknown here (I restarted)."""
        with self.cv:
            if epoch == self.epoch and seq == self.recv_seq:
                self.peer_speaking = False
                self.cv.notify_all()
            return epoch == self.epoch

    def on_hello(self):
        with self.cv:
            # Ignored before the first reply of an exchange: a hello that arrives
            # right after I opened is just a late retry from the peer's boot.
            if self.starter and self.recv_seq > 0:
                self.reopen = True
                self.cv.notify_all()

    # --- called from the main loop ---
    def new_epoch(self):
        with self.cv:
            self.epoch = uuid.uuid4().hex[:8]
            self.seen_epochs.add(self.epoch)
            self.sent_seq = self.recv_seq = 0
            self.inbox = None
            self.peer_speaking = False
            self.reopen = False
            return self.epoch

    def wait_message(self, timeout=None):
        with self.cv:
            self.cv.wait_for(lambda: self.inbox is not None or self.reopen, timeout=timeout)
            msg, self.inbox = self.inbox, None
            return msg

    def wait_peer_quiet(self, timeout):
        with self.cv:
            return self.cv.wait_for(lambda: not self.peer_speaking, timeout=timeout)

    def claim_turn(self, epoch, seq):
        """Mark seq as sent, unless the conversation has moved on without us."""
        with self.cv:
            if epoch != self.epoch or self.inbox is not None:
                return False
            self.sent_seq = seq
            return True


STAGE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage.html")


def make_handler(ch, role, peer):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")  # the stage page polls both nodes
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                epoch, seq = str(body.get("epoch")), int(body.get("seq", 0))
            except (ValueError, TypeError):
                self.send_error(400)
                return
            if self.path == "/say":
                self._reply({"accepted": ch.on_say(epoch, seq, str(body.get("text", "")))})
            elif self.path == "/done":
                self._reply({"known": ch.on_done(epoch, seq)})
            elif self.path == "/hello":
                ch.on_hello()
                self._reply({"ok": True})
            else:
                self.send_error(404)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/status":
                show = ch.show
                self._reply(dict(show, role=role, peer=peer, epoch=ch.epoch, now=time.time(),
                                 speaking=show["state"] == "speaking", env_hz=ENV_HZ))
            elif path in ("/", "/stage"):
                try:
                    with open(STAGE_HTML, "rb") as f:
                        body = f.read()
                except OSError:
                    self.send_error(404, "stage.html not found next to duet.py")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    return Handler


def post(url, payload, retry_for):
    """POST with retries, so the nodes can be started in any order.
    Returns the peer's JSON reply, or None if it could not be reached."""
    deadline = time.monotonic() + retry_for
    delay = 0.5
    while True:
        try:
            r = requests.post(url, json=payload, timeout=5)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if time.monotonic() > deadline:
                return None
            time.sleep(delay)
            delay = min(delay * 2, 5)


def wait_for_idle_peer(args, log):
    """At startup, don't talk over a peer that is still mid-sentence."""
    deadline = time.monotonic() + args.wait_timeout
    announced = False
    while time.monotonic() < deadline:
        try:
            if not requests.get(f"{args.peer}/status", timeout=3).json().get("speaking"):
                return
        except (requests.RequestException, ValueError):
            if not announced:
                log.info(f"waiting for peer at {args.peer} ...")
                announced = True
        time.sleep(1)


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def clean(text):
    """Tidy model output for speech."""
    t = re.sub(r"\*[^*]*\*", " ", text)                    # *actions*
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)            # (stage directions) [notes]
    t = re.sub(r"(?im)^\s*(light|dark)\s*:\s*", "", t)     # "DARK:" labels
    t = re.sub(r"[*_#`~>|\"“”«»]", "", t)                  # markdown and quotes
    t = re.sub(r"(?<!\w)['‘’]|['‘’](?!\w)", "", t)         # single quotes, keep apostrophes
    t = EMOJI.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—:;,")
    # Keep at most 3 sentences, and trim a dangling half-sentence.
    sentences = re.split(r"(?<=[.!?…])\s+", t)
    t = " ".join(sentences[:3])
    if t and t[-1] not in ".!?…":
        cut = max(t.rfind(c) for c in ".!?…")
        t = t[: cut + 1] if cut > 0 else t + "."
    return t


def build_messages(system, history):
    """System prompt + history, merging consecutive same-role messages."""
    msgs = []
    for m in history:
        if msgs and msgs[-1]["role"] == m["role"]:
            msgs[-1] = {"role": m["role"], "content": msgs[-1]["content"] + "\n" + m["content"]}
        else:
            msgs.append(dict(m))
    return [{"role": "system", "content": system}] + msgs


def generate(args, system, history, seq):
    if args.mock:
        time.sleep(random.uniform(0.1, 1.2))  # sometimes longer than the peer's speech
        return f"{args.role} line {seq}, {random.choice(['hm', 'yes', 'and yet', 'listen'])}."
    r = requests.post(
        f"{args.ollama}/api/chat",
        json={
            "model": args.model,
            "messages": build_messages(system, history),
            "stream": False,
            "keep_alive": -1,  # keep the model loaded between turns
            "options": {
                "temperature": args.temperature,
                "repeat_penalty": 1.15,
                "num_predict": 90,
            },
        },
        timeout=600,
    )
    r.raise_for_status()
    return clean(r.json()["message"]["content"])


def trim(history, keep):
    """Keep the last `keep` messages, always starting with a user message."""
    history = history[-keep:]
    while history and history[0]["role"] != "user":
        history = history[1:]
    return history


# --------------------------------------------------------------------------
# Speech. A line is rendered to audio first (while the peer is still talking),
# then panned and played when it's our turn. play() blocks until playback ends.
# --------------------------------------------------------------------------
ENV_HZ = 20  # loudness samples per second, sent to the stage page for the avatars


def voice_sample_rate(voice_path):
    with open(voice_path + ".json") as f:
        return int(json.load(f)["audio"]["sample_rate"])


def synthesize(args, text):
    """Render text to mono 16-bit PCM. Returns (pcm_bytes, sample_rate)."""
    if args.tts == "say":
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "line.wav")
            cmd = ["say", "-o", path, "--data-format=LEI16@22050"]
            if args.say_voice:
                cmd += ["-v", args.say_voice]
            if args.say_rate:
                cmd += ["-r", str(args.say_rate)]
            subprocess.run(cmd + ["-f", "-"], input=text.encode(), check=True, timeout=120,
                           capture_output=True)
            with wave.open(path) as w:
                return w.readframes(w.getnframes()), w.getframerate()

    cmd = [args.piper, "--model", args.voice, "--output-raw"]
    if args.length_scale:
        cmd += ["--length_scale", str(args.length_scale)]
    r = subprocess.run(cmd, input=(text + "\n").encode(), capture_output=True, check=True,
                       timeout=120)
    return r.stdout, args.sample_rate


def samples(pcm):
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def envelope(pcm, rate):
    """Loudness (0..1) at ENV_HZ, so the avatars can move with the voice."""
    a = samples(pcm)
    block = max(1, rate // ENV_HZ)
    env = []
    for i in range(0, len(a), block):
        chunk = a[i:i + block:4]  # every 4th sample is plenty for a loudness curve
        env.append(math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk))))
    peak = max(env, default=0) or 1
    return [round(min(1.0, e / peak), 2) for e in env]


def pan_stereo(pcm, pan):
    """Constant-power pan of mono PCM: -1 = hard left, 0 = centre, 1 = hard right."""
    mono = samples(pcm)
    angle = (max(-1.0, min(1.0, pan)) + 1) * math.pi / 4
    gl, gr = math.cos(angle), math.sin(angle)
    stereo = array("h", bytes(len(mono) * 4))
    stereo[0::2] = array("h", (int(x * gl) for x in mono))
    stereo[1::2] = array("h", (int(x * gr) for x in mono))
    if sys.byteorder == "big":
        stereo.byteswap()
    return stereo.tobytes()


# --------------------------------------------------------------------------
# Audio glitches: gentle, tape-like moves, never noise or static. Each glitch
# chains two or three moves (the tape dips down, speeds up, or skips), and the
# pitch moves glide in and out rather than switching on and off. The times go
# to the stage page, so the picture glitches together with the sound.
# --------------------------------------------------------------------------
MOVES = ["down", "up", "stutter"]


def to_pcm(a):
    a = array("h", a)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _clip(v):
    return -32768 if v < -32768 else 32767 if v > 32767 else int(v)


def _varispeed(x, speed_at):
    """Play through x like a tape whose speed changes as it goes. speed_at gets
    how far through x we are (0..1); speed > 1 is higher and faster."""
    out, pos, n = array("h"), 0.0, len(x) - 1
    while pos < n:
        j = int(pos)
        out.append(_clip(x[j] + (x[j + 1] - x[j]) * (pos - j)))
        pos += speed_at(pos / n)
    return out


def _glide(target, into=0.4, out_of=0.3):
    """Speed curve that eases from normal to target, holds, and eases back."""
    smooth = lambda u: u * u * (3 - 2 * u)
    def speed(u):
        k = smooth(min(1.0, u / into)) if u < into else smooth(min(1.0, (1 - u) / out_of)) if u > 1 - out_of else 1.0
        return 1 + (target - 1) * k
    return speed


def _fade(seg, n):
    """Short fade in and out, so cuts never click."""
    n = min(n, len(seg) // 2)
    for k in range(n):
        seg[k] = int(seg[k] * k / n)
        seg[-1 - k] = int(seg[-1 - k] * k / n)
    return seg


def apply_move(seg, move, rate):
    if move == "down":       # the tape drags: glides lower and slower, then recovers
        return _varispeed(seg, _glide(random.uniform(0.6, 0.72)))
    if move == "up":         # glides higher and faster, then settles
        return _varispeed(seg, _glide(random.uniform(1.3, 1.45)))
    # stutter: a grain skips like a scratched CD, each repeat shorter and a touch
    # higher, so the skip speeds up into the rest of the stretch
    out, edge, start = array("h"), int(0.005 * rate), 0
    for k, length in enumerate([0.11, 0.085, 0.065, 0.05, 0.04]):
        grain = seg[start:start + int(length * rate)]
        out.extend(_fade(_varispeed(grain, lambda u, k=k: 1 + 0.035 * k), edge))
    return out + _fade(seg[int(0.04 * rate):], edge)


def glitch_line(pcm, rate, chance):
    """Maybe glitch one or two short stretches of a line.
    Returns (pcm, events), events being [{"t": start s, "d": length s, "kind": "up>stutter"}]."""
    x = samples(pcm)
    if chance <= 0 or random.random() > chance or len(x) < rate:
        return pcm, []
    want, segs = random.choice([1, 1, 2]), []
    for _ in range(12):
        if len(segs) == want:
            break
        d = int(random.uniform(0.45, 0.9) * rate)
        lo, hi = int(0.1 * len(x)), int(0.9 * len(x)) - d
        if hi <= lo:
            break
        s0 = random.randint(lo, hi)
        if all(s0 + d < a or s0 > a + b for a, b in segs):
            segs.append((s0, d))
    out, events, cur, edge = array("h"), [], 0, int(0.004 * rate)
    for s0, d in sorted(segs):
        out.extend(_fade(x[cur:s0], edge))
        moves = random.sample(MOVES, random.choice([2, 2, 3]))
        start = len(out)
        bounds = [s0 + d * k // len(moves) for k in range(len(moves) + 1)]
        for move, a, b in zip(moves, bounds, bounds[1:]):
            out.extend(_fade(apply_move(x[a:b], move, rate), edge))
        events.append({"t": round(start / rate, 3), "d": round((len(out) - start) / rate, 3),
                       "kind": ">".join(moves)})
        cur = s0 + d
    out.extend(_fade(x[cur:], edge))
    return to_pcm(out), events


def play(args, pcm, rate):
    channels = 1
    if args.pan is not None:
        pcm, channels = pan_stereo(pcm, args.pan), 2
    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "line.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(channels)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm)
            subprocess.run(["afplay", path], check=True, timeout=180)
        return
    cmd = ["aplay", "-q", "-r", str(rate), "-f", "S16_LE", "-t", "raw", "-c", str(channels)]
    if args.audio_device:
        cmd += ["-D", args.audio_device]
    subprocess.run(cmd, input=pcm, check=True, timeout=180)


def prepare(args, text, log):
    """Render a line ahead of time. Returns (pcm, rate, envelope, glitches); pcm
    is None if there is nothing to play (mock mode, or synthesis failed)."""
    if args.mock:
        n = int(random.uniform(0.3, 0.9) * ENV_HZ)
        events = []
        if random.random() < args.glitch:
            events = [{"t": round(random.uniform(0, n / ENV_HZ * 0.6), 3), "d": 0.3,
                       "kind": ">".join(random.sample(MOVES, 2))}]
        return None, 0, [round(random.random(), 2) for _ in range(n)], events
    try:
        pcm, rate = synthesize(args, text)
        pcm, events = glitch_line(pcm, rate, args.glitch)
        return pcm, rate, envelope(pcm, rate), events
    except Exception as e:
        log.info(f"speech synthesis failed: {e}")
        return None, 0, [0.0] * int(0.07 * len(text) * ENV_HZ), []


def speak(args, ch, seq, text, audio, log):
    """Play a prepared line, updating the stage while it plays."""
    pcm, rate, env, glitches = audio
    ch.show = {"state": "speaking", "text": text, "seq": seq, "env": env, "glitches": glitches,
               "env_start": time.time() + 0.05}  # ~player start-up latency
    try:
        if pcm is None:
            time.sleep(len(env) / ENV_HZ)  # keep the rhythm going even without audio
        else:
            play(args, pcm, rate)
    except Exception as e:
        log.info(f"playback failed: {e}")
        time.sleep(len(env) / ENV_HZ)
    finally:
        ch.show = {"state": "idle", "text": text, "seq": seq, "env": [], "env_start": 0.0}


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Light & Dark: an endless spoken duet.")
    ap.add_argument("--role", choices=["light", "dark"], required=True)
    ap.add_argument("--peer", required=True, help="peer URL, e.g. http://dark.local:8000")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0", help="interface to listen on")
    ap.add_argument("--start", action="store_true", help="this node speaks first")
    ap.add_argument("--model", default="llama3.2:3b")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--keep", type=int, default=8, help="messages of context to keep")
    ap.add_argument("--nudge-every", type=int, default=5, help="0 disables provocations")
    ap.add_argument("--wait-timeout", type=float, default=120,
                    help="max seconds to wait for the peer's /done")
    ap.add_argument("--reply-timeout", type=float, default=300,
                    help="(--start node) seconds of silence before restarting the exchange")
    ap.add_argument("--tts", choices=["piper", "say"], default="piper")
    ap.add_argument("--voice", default="voices/en_US-lessac-medium.onnx", help="Piper .onnx voice")
    ap.add_argument("--piper", default="piper", help="path to the piper binary")
    ap.add_argument("--length-scale", type=float, default=None,
                    help="Piper speaking speed, >1 is slower (e.g. 1.2 for DARK)")
    ap.add_argument("--audio-device", default=None, help='aplay device, e.g. "plughw:1,0"')
    ap.add_argument("--say-voice", default=None, help="macOS voice, e.g. Samantha or Daniel")
    ap.add_argument("--say-rate", type=int, default=None, help="macOS words per minute")
    ap.add_argument("--glitch", type=float, default=0.5,
                    help="chance (0..1) that a line glitches; 0 = never")
    ap.add_argument("--pan", type=float, default=None,
                    help="stereo position, -1 left .. 1 right (default: mono)")
    ap.add_argument("--log-dir", default=".")
    ap.add_argument("--mock", action="store_true", help="fake generation and speech")
    ap.add_argument("--trace", action="store_true", help="log speech start/end timings")
    args = ap.parse_args()
    args.peer = args.peer.rstrip("/")
    return args


def main():
    args = parse_args()
    log = Log(args.role, args.log_dir, args.trace)

    if not args.mock and args.tts == "piper":
        try:
            args.sample_rate = voice_sample_rate(args.voice)
        except (OSError, KeyError, ValueError) as e:
            log.info(f"could not read {args.voice}.json ({e}); assuming 22050 Hz")
            args.sample_rate = 22050

    ch = Channel(starter=args.start)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(ch, args.role, args.peer))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"listening on :{args.port}, peer {args.peer}"
             f"{', speaking first' if args.start else ''}{' (mock)' if args.mock else ''}")

    history = []
    turn = 0
    first = True
    need_open = args.start

    if args.start:
        wait_for_idle_peer(args, log)
    else:
        # Tell the starter we (re)started, so it reopens instead of waiting out a timeout.
        threading.Thread(target=post, args=(f"{args.peer}/hello", {}, args.reply_timeout),
                         daemon=True).start()

    while True:
        # 1. Get something to respond to.
        if need_open:
            need_open = False
            epoch, seq = ch.new_epoch(), 1
            history.append({"role": "user", "content": OPENING if first else INTO_THE_SILENCE})
            first = False
        else:
            msg = ch.wait_message(timeout=args.reply_timeout if args.start else None)
            if msg is None:
                if ch.reopen:
                    log.info("peer (re)started, opening a fresh exchange")
                else:
                    log.info(f"no reply for {args.reply_timeout:.0f}s, restarting the exchange")
                need_open = True
                continue
            epoch, in_seq, text = msg
            history.append({"role": "user", "content": text})
            seq = in_seq + 1
        history = trim(history, args.keep)

        # 2. Generate and render audio, while the peer is (probably) still speaking.
        ch.show = dict(ch.show, state="thinking")
        system = PERSONAS[args.role] + " " + SHARED_RULES
        if args.nudge_every and turn > 0 and turn % args.nudge_every == 0:
            system += " For this reply: " + random.choice(PROVOCATIONS)
        try:
            reply = generate(args, system, history, seq)
        except Exception as e:
            log.info(f"generation failed: {e}")
            reply = ""
        reply = reply or random.choice(FALLBACKS[args.role])
        audio = prepare(args, reply, log)
        ch.show = dict(ch.show, state="ready")

        # 3. Wait for the peer to finish talking.
        if seq > 1 and not ch.wait_peer_quiet(args.wait_timeout):
            log.info(f"peer never sent /done after {args.wait_timeout:.0f}s, going on")
        if not ch.claim_turn(epoch, seq):
            log.info("conversation moved on while I was thinking; dropping my reply")
            ch.show = dict(ch.show, state="idle")
            continue

        # 4. Hand the text over, speak it, then pass the turn.
        resp = post(f"{args.peer}/say", {"epoch": epoch, "seq": seq, "text": reply},
                    retry_for=args.wait_timeout)
        if resp and not resp.get("accepted"):
            if not args.start:
                log.info("peer has moved on; dropping my reply")
                ch.show = dict(ch.show, state="idle")
                continue
            # The peer restarted and lost our thread: reopen with this same line.
            log.info("peer lost the thread, reopening the exchange")
            epoch, seq = ch.new_epoch(), 1
            ch.claim_turn(epoch, seq)
            resp = post(f"{args.peer}/say", {"epoch": epoch, "seq": seq, "text": reply},
                        retry_for=args.wait_timeout)
        if resp is None:
            log.info("peer unreachable; speaking anyway")
        log.line(reply)
        log.trace("START", seq)
        speak(args, ch, seq, reply, audio, log)
        log.trace("END", seq)
        resp = post(f"{args.peer}/done", {"epoch": epoch, "seq": seq}, retry_for=30)
        if args.start and resp is not None and not resp.get("known", True):
            log.info("peer restarted while I was speaking, opening a fresh exchange")
            need_open = True

        history.append({"role": "assistant", "content": reply})
        turn += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
