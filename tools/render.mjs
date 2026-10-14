#!/usr/bin/env node
// Render a recorded Light & Dark session (duet.py --record DIR) to video.
//
//   cd tools && npm install
//   node tools/render.mjs --session recordings/session1 --out renders/clip.mp4 \
//       [--start 0] [--dur 60] [--view both|light|dark] [--size 1920x1080] \
//       [--fps 30] [--captions]
//   node tools/render.mjs --session DIR --still --start S --out frame.png [--view ...]
//
// It opens stage.html in your installed Chrome (headless), steps the stage's
// virtual clock one frame at a time, pipes the frames to ffmpeg, and muxes in
// the session's audio (mixed by tools/mixdown.py). Start and duration are in
// seconds from the start of the session.
import fs from 'node:fs';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import puppeteer from 'puppeteer-core';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const opt = { start: 0, dur: null, view: 'both', size: null, fps: 30, captions: false };
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  const k = argv[i].replace(/^--/, '');
  if (k === 'captions' || k === 'still') opt[k] = true;
  else opt[k] = argv[++i];
}
if (!opt.session || !opt.out) {
  console.error('usage: node tools/render.mjs --session DIR --out FILE.mp4 [--start S] [--dur D] [--view both|light|dark] [--size WxH] [--fps N] [--captions]');
  process.exit(1);
}
const fps = Number(opt.fps), start = Number(opt.start);
const [W, H] = (opt.size || (opt.view === 'both' ? '1920x1080' : '1440x1080')).split('x').map(Number);

// Recorded events from both nodes.
const session = path.resolve(opt.session);
const events = fs.readdirSync(session).filter(f => /^timeline_.*\.jsonl$/.test(f))
  .flatMap(f => fs.readFileSync(path.join(session, f), 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)));
const t0 = Math.min(...events.map(e => e.start ?? e.t));
const end = Math.max(...events.filter(e => e.type === 'speak').map(e => e.start + e.dur));
const dur = opt.dur ? Number(opt.dur) : end - t0 - start;
const frames = Math.round(dur * fps);
fs.mkdirSync(path.dirname(path.resolve(opt.out)), { recursive: true });

// --still: one PNG frame at --start (the stage is warmed up for a couple of
// seconds first, so the heads are mid-motion rather than at rest).
if (opt.still) {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: true, args: ['--allow-file-access-from-files'] });
  const page = await browser.newPage();
  await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
  await page.goto(pathToFileURL(path.join(ROOT, 'stage.html')).href + '?playback=1');
  await page.evaluate(evs => window.loadTimeline(evs), events);
  for (let k = -60; k <= 0; k++) await page.evaluate((T, v, c) => window.renderAt(T, v, c), t0 + start + k / fps, opt.view, opt.captions);
  const png = await page.evaluate(() => document.getElementById('c').toDataURL('image/png').split(',')[1]);
  fs.writeFileSync(path.resolve(opt.out), Buffer.from(png, 'base64'));
  await browser.close();
  console.log(`wrote ${opt.out}`);
  process.exit(0);
}

// Audio first: one stereo mix of the same stretch.
const wav = path.resolve(opt.out).replace(/\.\w+$/, '') + '.wav';
execFileSync('python3', [path.join(ROOT, 'tools/mixdown.py'), session, wav, '--start', String(start), '--dur', String(dur)], { stdio: 'inherit' });

const browser = await puppeteer.launch({ executablePath: CHROME, headless: true, args: ['--allow-file-access-from-files'] });
const page = await browser.newPage();
await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });
await page.goto(pathToFileURL(path.join(ROOT, 'stage.html')).href + '?playback=1');
await page.evaluate(evs => window.loadTimeline(evs), events);

const ffmpeg = spawn('ffmpeg', [
  '-y', '-loglevel', 'error',
  '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'mjpeg', '-i', '-',
  '-i', wav,
  '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
  '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-shortest', '-movflags', '+faststart',
  path.resolve(opt.out),
], { stdio: ['pipe', 'inherit', 'inherit'] });

const t = Date.now();
for (let k = 0; k < frames; k++) {
  const T = t0 + start + k / fps;
  const jpeg = await page.evaluate((T, view, captions) => {
    window.renderAt(T, view, captions);
    return document.getElementById('c').toDataURL('image/jpeg', 0.95).split(',')[1];
  }, T, opt.view, opt.captions);
  if (!ffmpeg.stdin.write(Buffer.from(jpeg, 'base64'))) await new Promise(r => ffmpeg.stdin.once('drain', r));
  if (k % (fps * 10) === 0) process.stdout.write(`\rframe ${k}/${frames} (${((Date.now() - t) / 1000).toFixed(0)}s)`);
}
ffmpeg.stdin.end();
await new Promise(r => ffmpeg.on('close', r));
await browser.close();
fs.unlinkSync(wav);
console.log(`\nwrote ${opt.out} (${dur.toFixed(1)}s, ${W}x${H}, ${opt.view})`);
