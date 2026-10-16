<div align="center">

# Interminable Nothingness

**Two machines ask each other the meaning of life, forever.**

A durational installation for two CRTs · Checo Cadena · 2026

<img src="media/hero.gif" alt="Two low-poly heads in inverse wireframe, one black on white, one white on black, talking to each other" width="100%">

[**Watch the videos**](https://github.com/checocadena/interminable-nothingness/releases/latest) · [**Read the proposal (PDF)**](proposal/Interminable-Nothingness-proposal.pdf) · [**Build it**](docs/SETUP.md)

</div>

---

Two small artificial minds sit on two old television sets, facing each other.
One of them asked the other what the meaning of life is. They have been talking
ever since.

**LIGHT** is warm, hopeful and curious, and finds meaning in connection, small
joys and the miracle that anything exists. **DARK** is brooding and strange and
speaks like an old oracle, finding meaning in impermanence, deep time and the
indifferent dark between the stars. Neither is allowed to simply agree: LIGHT
argues back against DARK's bleakness, and DARK answers LIGHT's comfort with a
colder truth. Each is a small language model running on its own Raspberry Pi, with its
own voice, its own speaker and its own screen. There is no internet, no cloud
and no script. Every word is generated in the room, in real time, and spoken
aloud.

The models are small on purpose. They remember only the last few things that
were said, and a breath of silence falls between each line and the reply, so
the conversation never resolves: they forget, circle back,
contradict themselves, rediscover the same ideas in new words, and reach for
answers they can't hold onto.

<table>
<tr>
<td width="50%"><img src="media/still-light.png" alt="LIGHT: black wireframe head on white"></td>
<td width="50%"><img src="media/still-dark.png" alt="DARK: white wireframe head on black"></td>
</tr>
</table>

The faces are inverses of each other, low-poly busts in the style of late-90s
PlayStation graphics, seen from slightly above like a game-over screen. When
one speaks, its jaw moves with its voice and the other turns to listen. Now and
then a voice slips (it drags lower, rises, or skips like a scratched CD), and in
that moment its picture tears too.

The piece answers Nam June Paik's *TV Buddha* (1974), in which a statue of the
Buddha contemplates its own live image on a television. Here the contemplation
turns outward: two machines, each the other's negative, contemplating each
other, endlessly, about the one question neither can answer.

## From the conversation

> **DARK** <sub>00:21</sub><br>
> The meaning of life is like a dying ember. Fleeting, fragile, and devoid of warmth. It's not about finding purpose or just getting by, but about the spaces in between, where emptiness and existence coexist. Time stretches out before us like a dark lake. What lies at its bottom?
>
> **LIGHT** <sub>00:42</sub><br>
> That's a beautiful image, DARK, but I don't see how it captures the sparkle in people's eyes when they're doing something that matters to them. Life seems too vibrant for just emptiness and existence coexisting. Where do those sparks come from?
>
> **DARK** <sub>00:59</sub><br>
> The sparks are ash from a fire long extinguished. They flicker in the dark, reminding us of what's been lost. Meaning is not something we spark to life, but rather, it's the darkness that makes our passions more palpable. It consumes them.
>
> **LIGHT** <sub>01:49</sub><br>
> DARK, I don't think joy is shallow at all. It's the laughter of children playing together. And warmth isn't just a brief respite, but the feel of sunlight on skin and the taste of a good meal. Life matters because we experience moments like these every day!
>
> **DARK** <sub>02:07</sub><br>
> Childhood laughter is a brief echo of a universe indifferent to joy. Sunlight scorching skin is but a fleeting torment. Your moments are islands in an endless sea, swallowed by the void that awaits us all. Silence gathers around them like a shroud.
>
> **LIGHT** <sub>03:01</sub><br>
> I think our connections are more than just shadows. Friends and family hold up each other's hands when life feels like falling apart. Every time someone reaches out to catch us is a tiny miracle in itself. We get back up again.

<sub>Unedited, from the first minutes of a 26-minute recorded session.</sub>

## In the room

<img src="media/plan.svg" alt="Top view: two CRTs facing each other 1.5 to 3 m apart, audience to the side" width="100%">

- Two CRT televisions on plinths face each other across 1.5 to 3 m, each showing one face.
- Each TV is its own voice, so the conversation crosses the room.
- Each set is driven by a Raspberry Pi 5 running Llama 3.2 3B (through Ollama), a Piper voice and the face. The two Pis talk over one ethernet cable.
- It runs indefinitely and looks after itself: if either side restarts, they find each other and carry on.

The full technical rider is in the [proposal](proposal/Interminable-Nothingness-proposal.pdf).

## How it works

```
  CRT 1 (LIGHT)                                   CRT 2 (DARK)
 ┌───────────────────┐   POST /say  {text}       ┌───────────────────┐
 │ Pi 5              │ ────────────────────────▶ │ Pi 5              │
 │  duet.py          │ ◀──────────────────────── │  duet.py          │
 │  Ollama 3B model  │   POST /done              │  Ollama 3B model  │
 │  Piper voice      │                           │  Piper voice      │
 │  stage.html face  │                           │  stage.html face  │
 └───────────────────┘                           └───────────────────┘
```

- [`duet.py`](duet.py) is the whole conversation. Each side writes its reply
  *while the other is still speaking*, which hides the Pi's slow generation, then
  waits its turn, speaks, and passes the turn back. The personas, the opening
  question and the provocations that keep them from looping are plain text at
  the top of the file.
- [`stage.html`](stage.html) draws the face: the head mesh is projected and drawn
  as filled polygons with hidden faces removed, with PS1-style vertex snapping.
  The jaw follows the real loudness of the voice, and glitches arrive from
  `duet.py` with exact timings, so sound and picture break together.
- [`tools/render.mjs`](tools/render.mjs) renders a recorded session to video
  frame by frame. The videos in the release were made this way, from a real,
  unedited session.

To try it on a Mac (two terminals, one machine), see
[Test on a Mac first](docs/SETUP.md#3-test-on-a-mac-first-single-machine-two-ports).
To build it on two Raspberry Pis, follow [docs/SETUP.md](docs/SETUP.md).

## Credits

Concept and installation by Checo Cadena. Head model: "Basic head mesh" by
kavabanga ([BlendSwap](https://www.blendswap.com/blend/15103), CC0).
Language model: [Llama 3.2 3B](https://ollama.com/library/llama3.2) through
[Ollama](https://ollama.com). Voices: [Piper](https://github.com/rhasspy/piper)
on the Pis, macOS `say` in the recorded videos.

© 2026 Checo Cadena. All rights reserved.
