# Setup and reference

How to build *Interminable Nothingness* on two Raspberry Pi 5s, test it on a
Mac, and record it to video. For what the piece is, see the
[README](../README.md).

Two small language models, LIGHT and DARK, run one per Pi. They exchange text
over the LAN and each one speaks its lines with its own local Piper voice.
There is no speech recognition.

```
   Pi "light"                                   Pi "dark"
 ┌──────────────┐   POST /say {text}         ┌──────────────┐
 │ duet.py      │ ─────────────────────────▶ │ duet.py      │
 │ Ollama 3B    │ ◀───────────────────────── │ Ollama 3B    │
 │ Piper → aplay│   POST /done               │ Piper → aplay│
 └──────┬───────┘                            └──────┬───────┘
     speaker                                     speaker
```

Each node generates its reply while the other one is still speaking, which
hides most of the Pi's slow generation time.

## Files

| File | What it is |
|---|---|
| `duet.py` | The whole installation. Personas, the opening line and provocations are constants at the top. |
| `systemd/duet-light.service`, `systemd/duet-dark.service` | Start the duet automatically at boot. |
| `stage.html` | The visual stage: two inverse low-poly heads, one per CRT. Served by `duet.py` at `http://<node>:<port>/`. |
| `tools/render.mjs`, `tools/mixdown.py` | Render a recorded session to video (see [Recording and rendering video](#recording-and-rendering-video)). |
| `mock_test.py` | Protocol test: both roles on localhost with fake models and fake speech. It kills each node once and checks the voices never overlap. |

## 1. Prepare each Pi (fresh Raspberry Pi OS, 64-bit)

Flash **Raspberry Pi OS (64-bit, Bookworm or later)** with Raspberry Pi Imager.
In the Imager settings, set the hostnames to **`light`** on one card and
**`dark`** on the other. Also set the user to `pi`, and set Wi-Fi if you're not
using Ethernet. The Pis can then find each other as `light.local` and
`dark.local` without fixed IPs.

On **both** Pis:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-requests curl alsa-utils
mkdir -p ~/light-and-dark/voices && cd ~/light-and-dark
# copy duet.py (and the systemd/ folder) into ~/light-and-dark, e.g. with scp:
#   scp -r duet.py systemd pi@light.local:~/light-and-dark/
```

`requests` is the only Python dependency. The apt package avoids pip's
"externally managed environment" error on Pi OS.

### Ollama and the model

```bash
curl -fsSL https://ollama.com/install.sh | sh     # installs and starts ollama.service
ollama pull llama3.2:3b
ollama run llama3.2:3b "Say hello in one sentence."   # sanity check, Ctrl+D to exit
```

On a Pi 5, a 3B model produces a short reply in roughly 5 to 20 seconds. For
faster turns, try `--model llama3.2:1b` or `--model qwen2.5:1.5b`. Pull the
model first.

### Piper (text to speech)

Use the prebuilt ARM64 release:

```bash
cd ~/light-and-dark
curl -L -o piper.tar.gz \
  https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz
tar xzf piper.tar.gz && rm piper.tar.gz      # creates ./piper/piper
```

### Voices: a different one on each Pi

Each voice is an `.onnx` file plus its `.onnx.json` (the sample rate is read
from the JSON). Download into `~/light-and-dark/voices/`.

On **light**:

```bash
cd ~/light-and-dark/voices
V=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium
curl -L -O $V.onnx -O $V.onnx.json
```

On **dark**:

```bash
cd ~/light-and-dark/voices
V=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium
curl -L -O $V.onnx -O $V.onnx.json
```

You can browse other voices at https://rhasspy.github.io/piper-samples/. Any
voice works as long as both files are downloaded.

### Find the USB audio device

```bash
aplay -l
```

Look for your USB speaker or sound card, e.g.

```
card 1: Device [USB Audio Device], device 0: USB Audio [USB Audio]
```

That is `--audio-device plughw:1,0` (card 1, device 0). Use `plughw`, not `hw`,
so ALSA resamples to whatever the device supports. Test it and set the volume:

```bash
speaker-test -D plughw:1,0 -c 2 -t wav -l 1
alsamixer -c 1
sudo alsactl store          # keep the volume after reboot
```

Card numbers can change if you plug in other USB audio devices. If that
happens, use the card name instead: `--audio-device plughw:CARD=Device,DEV=0`.

### Test speech on one Pi

```bash
cd ~/light-and-dark
echo "The dark between the stars is patient." | ./piper/piper --model voices/en_GB-alan-medium.onnx --output-raw \
  | aplay -D plughw:1,0 -r 22050 -f S16_LE -t raw -c 1
```

## 2. Run it

On **dark** (it can be started first or second, order doesn't matter):

```bash
cd ~/light-and-dark
python3 duet.py --role dark --peer http://light.local:8000 \
  --piper ./piper/piper --voice voices/en_GB-alan-medium.onnx \
  --length-scale 1.15 --audio-device plughw:1,0
```

On **light** (`--start` = speaks first):

```bash
cd ~/light-and-dark
python3 duet.py --role light --start --peer http://dark.local:8000 \
  --piper ./piper/piper --voice voices/en_US-lessac-medium.onnx \
  --audio-device plughw:1,0
```

Each node prints its own lines (`LIGHT: ...`) and appends them with timestamps
to `duet_light.log` / `duet_dark.log`.

If `.local` names don't resolve on your network, use IPs instead
(`hostname -I` on each Pi): `--peer http://192.168.1.42:8000`. Port 8000 must
be reachable between the Pis (no firewall is enabled on Pi OS by default).

### Useful flags

| Flag | Default | |
|---|---|---|
| `--model` | `llama3.2:3b` | any Ollama model |
| `--temperature` | `1.0` | |
| `--keep` | `8` | messages of context sent to the model |
| `--nudge-every` | `5` | add a random provocation every N turns (0 = off) |
| `--wait-timeout` | `120` | max seconds to wait for the peer's `/done` |
| `--reply-timeout` | `300` | (starter only) seconds of silence before it reopens the conversation |
| `--length-scale` | voice default | Piper speed; >1 is slower (nice for DARK) |
| `--glitch` | `0.5` | chance a line glitches (0 = never, 1 = every line) |
| `--pan` | mono | stereo position, -1 hard left .. 1 hard right (LIGHT left, DARK right) |
| `--tts say --say-voice X --say-rate N` | | macOS speech instead of Piper |
| `--mock` | | fake generation and speech (protocol testing) |

## 3. Test on a Mac first (single machine, two ports)

Install Ollama for macOS from https://ollama.com, then:

```bash
ollama pull llama3.2:3b
pip3 install requests
```

Terminal 1:

```bash
python3 duet.py --role dark --port 8002 --peer http://127.0.0.1:8001 --tts say --say-voice Daniel --say-rate 140 --pan 0.5
```

Terminal 2:

```bash
python3 duet.py --role light --port 8001 --peer http://127.0.0.1:8002 --start --tts say --say-voice Samantha --say-rate 155 --pan -0.5
```

Both nodes share the one local Ollama. List the installed voices with
`say -v '?'`.

Protocol only, no model and no audio:

```bash
python3 mock_test.py
```

## The stage (two CRTs)

Each head gets its own screen. The idea is two CRTs facing each other, after
Nam June Paik's *TV Buddha*. Each one is a blocky, low-poly, PS1-style bust:
a stoic face with no eyes, a heavy brow, strong cheekbones, a short straight
nose and a square jaw, on a neck and shoulders. The two are drawn as inverses
of each other: LIGHT is black
lines on white, DARK is white lines on black. The camera sits a little above
each face and drifts slowly around it, like a game-over screen. LIGHT is seen
from one side and DARK from the other, so the two screens mirror each other.

```
http://light.local:8000/?role=light     # CRT 1
http://light.local:8000/?role=dark      # CRT 2
http://127.0.0.1:8001/                  # Mac preview: both screens side by side
```

The node serving the page tells it where its peer is, and the page polls both
nodes. You can also point it at the nodes explicitly:
`?role=dark&light=http://light.local:8000&dark=http://dark.local:8000`.

Each head runs procedural animations, chosen from the duet's state:

- **Speaking:** the jaw opens with the real loudness of the voice (the inside of
  the mouth is solid ink), with nods on the stressed syllables.
- **Listening:** the head tilts, with small nods when the other voice gets loud.
- **Thinking:** the head bows and turns away.
- **Idle:** the head looks around, or paces back and forth past the camera.

**Glitches you can hear.** When a node renders a line, it may glitch one or
two short stretches of it (`--glitch`, default 0.5 = half the lines; 0 turns it
off). Each glitch chains two or three tape-like moves in a random order, and the
pitch moves glide in and out rather than switching. There's never static.

- **down:** the tape drags lower and slower, then recovers
- **up:** it speeds up higher, then settles
- **stutter:** a skip repeats, each repeat shorter and a touch higher, then
  carries on

The node sends the times to the stage, and only the face that's talking
glitches, swelling and fading with the sound. A face that isn't talking
flickers only very rarely (about once every one to two minutes, and never while
it listens). Samples are in [`glitch_samples/`](../glitch_samples/).

Starting to speak is a short hard cut; other changes of animation ease across.
During a glitch the head stutters through random poses while slices of the
screen slip sideways and flip into their opposite. The vertices snap to a coarse grid,
which gives the PS1 wobble. If a node goes offline, its picture collapses to a
dot like a CRT switching off, and the page opens with the same effect in
reverse.

The head is "Basic head mesh" by kavabanga from BlendSwap (#79148, CC0, the
original `.blend` is in [`Basic head mesh/`](../Basic%20head%20mesh/)). It was exported with Blender and
embedded in `stage.html` as `MESH`, keeping its four-sided faces. The
shoulders under it are built in code. `MESH_TILT` sets how far the chin is
raised.

The earlier head modelled in code is still available: add `&head=classic`
to the URL. Its shape is set by `ROWS`, `SLIM`, `TALL` and `BLOCKY` in
`buildHead()`.

Keys: **F** or double-click toggles fullscreen, **S** toggles captions (off by
default on a single screen; add `&captions=1` to start with them on), and
**1 / 2 / 0** switch between LIGHT, DARK and both.

### Putting the stage on the CRTs

Each Pi shows its own face full screen in Chromium. The Pi 5 has no composite
video jack, so a CRT needs an HDMI-to-composite (AV) converter from the Pi's
micro-HDMI port, or an HDMI-to-component/SCART converter for TVs that take
those. Set the Pi's output to 640x480 so the converter doesn't have to
downscale. The page is laid out for 4:3.

To open the face at login (Raspberry Pi OS Bookworm, labwc desktop), on
**light**:

```bash
mkdir -p ~/.config/labwc
echo 'chromium-browser --kiosk --noerrdialogs --disable-infobars "http://localhost:8000/?role=light" &' >> ~/.config/labwc/autostart
```

On **dark**, the same with `?role=dark`. This part hasn't been tested on a Pi
yet. Chromium and a 3B model share the Pi's memory, so use the 8 GB model, or
`--model llama3.2:1b` if it struggles.

## 4. Start at boot (systemd)

The unit files assume user `pi` and the folder `/home/pi/light-and-dark`. Edit
`User=`, the paths, `--audio-device` and the voice if yours differ.

On **light**:

```bash
sudo cp ~/light-and-dark/systemd/duet-light.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now duet-light
```

On **dark**:

```bash
sudo cp ~/light-and-dark/systemd/duet-dark.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now duet-dark
```

Watch it, stop it, restart it:

```bash
journalctl -u duet-light -f          # or duet-dark
sudo systemctl stop duet-light
sudo systemctl restart duet-light
```

The units start after the network and Ollama, wait until Ollama answers, and
restart automatically (`Restart=always`) if the script ever exits.

## Recording and rendering video

A session can be recorded and turned into video, frame by frame, without
screen capture. On a Mac, record silently (the pacing stays real-time, but
nothing plays):

```bash
python3 duet.py --role dark --port 8002 --peer http://127.0.0.1:8001 --tts say --say-voice Daniel --say-rate 140 --pan 0.5 --silent --record recordings/session1 --log-dir recordings/session1
python3 duet.py --role light --port 8001 --peer http://127.0.0.1:8002 --start --tts say --say-voice Samantha --say-rate 155 --pan -0.5 --silent --record recordings/session1 --log-dir recordings/session1
```

Each node writes `timeline_<role>.jsonl` (when it thought and spoke, the
loudness curve and glitches of every line) and the audio of every line into
the folder. To render, install the renderer's one dependency (puppeteer-core,
which drives your installed Chrome; it isn't needed to run the installation):

```bash
cd tools && npm install && cd ..
node tools/render.mjs --session recordings/session1 --out renders/both.mp4 --start 120 --dur 90 --view both
node tools/render.mjs --session recordings/session1 --out renders/light.mp4 --start 120 --dur 90 --view light
```

`--start` and `--dur` are seconds into the session. `--view` is `both`
(side by side, 1920x1080) or one face (`light` or `dark`, 1440x1080, 4:3 for
a CRT). `--captions` burns in the words. The stage runs in a playback mode
(`stage.html?playback=1`) driven by the recording instead of live nodes.

## How the turn-taking stays safe

- Every line carries an *epoch* (one continuous conversation) and a sequence
  number. Duplicates from retried POSTs and stale lines from before a restart
  are ignored, so the two voices never talk over each other.
- POSTs retry, so the Pis can boot in any order.
- If the peer never sends `/done` (it crashed mid-sentence), the wait gives up
  after `--wait-timeout` seconds.
- The `--start` node keeps the conversation going. If DARK restarts, it says
  hello and LIGHT immediately reopens the conversation. If the line simply goes
  quiet for `--reply-timeout` seconds, LIGHT speaks into the silence to draw
  DARK back.
- If generation fails, the node speaks a fallback line and carries on.

## Tuning the art

Everything you'd want to tweak is at the top of `duet.py`: `LIGHT_PERSONA`,
`DARK_PERSONA`, `SHARED_RULES`, `OPENING`, `PROVOCATIONS` (just add lines) and
`FALLBACKS`.
