# voice-stream

Continuous, local, hands-free dictation for Wayland — built for the case where you
keep talking to an agent all day and a hotkey has become the bottleneck.

Everything runs on the machine: no audio leaves it, no audio is written to disk,
and the only network call is the one-time model download.

## What it does

One long-lived microphone stream feeds a ring buffer. A silence-based segmenter cuts
utterances out of that buffer — **with 1.5 s of pre-roll**, so the first syllable is
never lost. Each segment is gated acoustically, transcribed locally with SenseVoice,
cleaned of filler words, and pasted into the focused window.

A small overlay in the corner shows what the gate decided.

## Design notes (the interesting part)

The hard question in always-on dictation is not transcription, it is **"is this
utterance meant for the machine?"**. Some conclusions, in case they save someone
else time.

### 1. Text-only intent classification does not work

An open Jev-style decision model (Decima-small, 122 M, int8 ONNX, ~20 ms,
Apache-2.0) was asked *"is this sentence addressed to the assistant?"* on 16
labelled utterances:

| question style | accuracy |
|---|---|
| short choice | 8/16 |
| verify (yes/no) | 9/16 |
| descriptive choice | 8/16 |

Confidence was 0.82–0.92 while the answers were wrong. The model is not bad; the
input has no signal. "帮我把这个报错查一下" and "唉，又报错了" are the same sentence
acoustically and lexically — only context separates them. Adding a situation note
("the assistant answered 5 s ago") did not fix it.

**Conclusion:** gate on acoustics and state, never on text semantics.

### 2. Level separates, but not because of distance alone

From 108 logged utterances (40 dictated vs 68 ambient):

| | peak (median) | RMS | SNR |
|---|---|---|---|
| dictated | −7.9 dB | −25.4 dB | 38.1 dB |
| ambient conversation | −17.0 dB | −34.4 dB | 29.8 dB |

A 9 dB gap — real, but overlapping. Far-field speech with automatic gain climbs
into the same band; level alone gives at best ~88 % precision / 72 % recall.

### 3. High-frequency energy is the better distance cue

Distance attenuates high frequencies faster than overall level, and mic AGC cannot
restore them. Share of energy in 2–6 kHz:

| condition | HF ratio | spectral centroid |
|---|---|---|
| speaking into the mic | 0.08 – 0.22 | 900 – 1280 Hz |
| standing beside it | 0.00 – 0.03 | 160 – 420 Hz |

Caveat: the ratio is meaningless at low levels (room noise is itself bright), so the
level gate must run **before** the spectral one.

### 4. Spectral flatness removes handling noise

Picking the mic up, rubbing it or typing near it produces broadband bursts:

| signal | flatness (geometric / arithmetic mean of the spectrum) |
|---|---|
| voiced speech | 0.00 – 0.20 |
| white noise / rustle | 0.56 |

Threshold 0.35, combined with zero-crossing rate.

### 5. Rejected approaches

- **Wake word** — reliable, but turns the interaction back into "Hey Siri".
- **Pickup gesture detection** — fragile, and adds a state the user must reason about.
- **Speaker verification (voiceprint)** — model prepared (3D-Speaker, 192-dim) but not
  enabled: it answers "who is speaking", not "who are you speaking to".
- **wtype as the output backend** — wtype 0.4 remaps keycodes per non-ASCII character;
  on Hyprland this occasionally emits a stray F1, opening the Chromium help page
  mid-sentence. Clipboard + Shift+Insert instead.

## Gate rules (current values)

A segment is transcribed and pasted only if all of these hold:

| rule | value | why |
|---|---|---|
| segment peak | ≥ −18 dB | below this it is far-field or the mic is muted |
| HF ratio (2–6 kHz) | ≥ 0.02 | distance cue, secondary check |
| voiced frames (ZCR < 0.25) | ≥ 20 % | rejects rustle and clicks |
| spectral flatness | < 0.35 | rejects broadband noise |
| transcript | not empty, not punctuation only | ASR noise |
| words | not a single English word under 1 s | stray "Okay." from transients |
| focused window class | default `dsh-cdp` | never type into random windows |

Time constants:

| parameter | value | effect |
|---|---|---|
| pre-roll | 1.5 s | audio kept before the trigger |
| end of segment | 1.5 s of silence | pause tolerance vs latency |
| early commit | after 0.6 s pause, when the transcript already ends with punctuation | cuts latency |
| max segment | 40 s | safety |
| live verdict | every 0.5 s over the last 3 s | overlay turns green while you speak |

### Filler cleanup

Rule-based and deliberately conservative:

- isolated 嗯/啊/呃/额/哦/唔 at either end are removed
- 那个 / 就是说 / 然后就是 only when followed by punctuation, so "那个文件" survives
- stutter collapse only for pronouns and particles (我我 → 我); real reduplication
  such as 看看 / 试试 is preserved
- leftover leading or trailing punctuation is trimmed

## Feedback

The overlay is a GTK4 layer-shell surface in the bottom-right corner:

| state | look | meaning |
|---|---|---|
| listening | gray-blue pulse | capturing, verdict pending |
| ok | green pulse | acoustics already look like speech into the mic |
| accepted | green flash | text was pasted |
| idle | hidden | — |

Stale state is ignored after 4 s, so a stopped service can never leave a green
indicator on screen.

## Device binding

The service is bound to a named microphone (`VOICE_STREAM_DEVICE_NAME`, default
`DJI`). `pw-record --target` receives the resolved node, so a Bluetooth disconnect
that flips the default source to the internal mic cannot silently redirect capture.

A timer checks every 3 s: device present and service stopped → start; device gone →
stop.

## Requirements

- Wayland compositor with layer-shell support (tested on Hyprland / Omarchy)
- PipeWire (`pw-record`, `pactl`)
- `wl-clipboard` (`wl-copy`) and `wtype`
- GTK4, `gtk4-layer-shell`, PyGObject (for the overlay)
- Python 3.11+

## Install

```bash
git clone https://github.com/<you>/voice-stream
cd voice-stream
./install.sh
```

The installer creates a venv under `~/.local/share/voice-stream`, downloads the
SenseVoice int8 model (~160 MB), renders the systemd units, and starts the overlay
and the microphone watcher.

```bash
systemctl --user status voice-stream
tail -f ~/.local/share/voice-stream/stream.log
```

## Configuration

Everything is an environment variable set in the systemd unit:

| variable | default | meaning |
|---|---|---|
| `VOICE_STREAM_DEVICE_NAME` | `DJI` | substring matched against PipeWire source description/name |
| `VOICE_STREAM_WINDOW` | `dsh-cdp` | required focused window class; empty = any window |
| `VOICE_STREAM_END_MS` | `1500` | silence before a segment closes |
| `VOICE_STREAM_PRE_ROLL` | `1.5` | seconds of audio kept before the trigger |
| `VOICE_STREAM_MIN_SPEECH` | `0.2` | minimum voiced duration |
| `VOICE_STREAM_SEG_PEAK` | `-18` | level floor in dBFS |
| `VOICE_STREAM_HF_MIN` | `0.02` | high-frequency ratio floor |
| `VOICE_STREAM_FLAT_MAX` | `0.35` | spectral flatness ceiling |
| `VOICE_STREAM_HOME` | `~/.local/share/voice-stream` | app dir (model, log, state) |

## Troubleshooting

- **Nothing is transcribed.** Check `stream.log`; every segment logs `peak`, `hf`,
  `voiced`, `flat` and a `reason` (`quiet`, `dull`, `unvoiced`, `flat`, `noise`,
  `punct`, `window`, `empty`).
- **Green but no text.** The acoustic gate passed but the transcript failed a text
  rule; the `reason` field says which.
- **Service not running.** The watcher keeps it stopped while the bound microphone is
  absent; `pactl list short sources` should show it.
- **Overlay invisible.** It needs `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so`
  because of the GTK/Wayland link order.

## Privacy

- No audio is written to disk.
- The log stores metadata only: duration, levels, decision, reason.
- The model runs locally; the only network access is the model download in install.sh.

## License

MIT
