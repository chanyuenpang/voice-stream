
#!/usr/bin/env python3
"""voice-stream: continuous local dictation.

One long-lived capture stream feeds a ring buffer. Utterances are cut out of that
buffer with 1.5 s of pre-roll, so nothing before the trigger is lost.

A first transcription pass runs as soon as the speaker pauses briefly; when the
result already ends with sentence punctuation it is committed right away instead
of waiting for the full silence window.
"""
import array, json, math, os, re, subprocess, sys, time
from collections import deque
from datetime import datetime

import numpy as np
import sherpa_onnx

SR = 16000
FRAME = SR * 20 // 1000
APP_DIR = os.path.expanduser(os.environ.get("VOICE_STREAM_HOME", "~/.local/share/voice-stream"))
MODEL_DIR = os.path.expanduser(os.environ.get("VOICE_STREAM_MODEL", APP_DIR + "/sensevoice-small"))
PRE_ROLL_S = float(os.environ.get("VOICE_STREAM_PRE_ROLL", "1.5"))
END_MS = int(os.environ.get("VOICE_STREAM_END_MS", "1500"))
MIN_SPEECH_S = float(os.environ.get("VOICE_STREAM_MIN_SPEECH", "0.2"))
MAX_SEG_S = float(os.environ.get("VOICE_STREAM_MAX_SEG", "40"))
ONLY_WINDOW = os.environ.get("VOICE_STREAM_WINDOW", "dsh-cdp")
DEVICE_MATCH = os.environ.get("VOICE_STREAM_DEVICE_NAME", "DJI")
LOG = os.path.expanduser(os.environ.get("VOICE_STREAM_LOG", APP_DIR + "/stream.log"))
STATE = os.path.expanduser(os.environ.get("VOICE_STREAM_STATE", APP_DIR + "/state"))
SPEECH_DB = -42.0
EARLY_SILENCE_FRAMES = 30          # 600 ms pause before a speculative pass
END_PUNCT = "。！？；.!?;…"

HF_MIN = float(os.environ.get("VOICE_STREAM_HF_MIN", "0.02"))
SEG_PEAK_MIN = float(os.environ.get("VOICE_STREAM_SEG_PEAK", "-18"))

def hf_ratio(buf):
    """Share of energy in 2-6 kHz: close speech is bright, far-field is dull."""
    if len(buf) < SR * 0.3:
        return 0.0
    a = np.frombuffer(buf.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
    win = np.hanning(len(a))
    sp = np.abs(np.fft.rfft(a * win)) ** 2
    freqs = np.fft.rfftfreq(len(a), 1 / SR)
    tot = sp.sum() + 1e-12
    return float(sp[(freqs >= 2000) & (freqs <= 6000)].sum() / tot)

FLAT_MAX = float(os.environ.get("VOICE_STREAM_FLAT_MAX", "0.35"))

def flatness(buf):
    """Spectral flatness: near 1 for noise, low for voiced/formant-rich speech."""
    if len(buf) < SR * 0.3:
        return 1.0
    a = np.frombuffer(buf.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
    sp = np.abs(np.fft.rfft(a * np.hanning(len(a)))) ** 2 + 1e-12
    g = np.exp(np.mean(np.log(sp)))
    return float(g / (np.mean(sp) + 1e-12))

def zcr(fr):
    """Zero-crossing rate: voiced speech is low, rustle/thumps are high."""
    n = len(fr)
    if n < 2:
        return 1.0
    c = 0
    prev = fr[0]
    for x in fr[1:]:
        if (x >= 0) != (prev >= 0):
            c += 1
        prev = x
    return c / (n - 1)

def resolve_device():
    """Find the bound microphone by name; None when it is not connected."""
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                             capture_output=True, text=True, timeout=3).stdout
        data = json.loads(out or "[]")
    except Exception:
        return None
    m = DEVICE_MATCH.lower()
    for s in data:
        if "monitor" in s.get("name", ""):
            continue
        if m in (s.get("description", "") + " " + s.get("name", "")).lower():
            return s.get("name")
    return None

def set_state(value):
    try:
        with open(STATE, "w") as f:
            f.write(value)
    except Exception:
        pass

def peak_db(xs):
    p = max((abs(x) for x in xs), default=0) / 32768
    return 20 * math.log10(p) if p > 0 else -120.0

def active_class():
    try:
        out = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=1).stdout
        return json.loads(out or "{}").get("class", "")
    except Exception:
        return ""

DRY = os.environ.get("VOICE_STREAM_DRY") == "1"

def meaningful(text):
    return bool(re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text))

SHORT_FILLERS = ("嗯", "啊", "呃", "额", "哦", "唔")
LONG_FILLERS = ("那个", "就是说", "然后就是")

def clean(text):
    """Remove isolated fillers and stutter repetitions, keep everything else."""
    t = text.strip()
    changed = True
    while changed:
        changed = False
        pairs = [(f, ("", "，", ",", "。", " ")) for f in SHORT_FILLERS]
        pairs += [(f, ("，", ",", "。", " ")) for f in LONG_FILLERS]
        for f, seps in pairs:
            for sep in seps:
                for pat in (f + sep, sep + f):
                    if pat and (t.startswith(pat) or t.endswith(pat)):
                        t2 = t[len(pat):] if t.startswith(pat) else t[: -len(pat)]
                        if meaningful(t2):
                            t = t2.strip(); changed = True
    # stutter: only collapse tokens that are stutters, never real reduplication
    # such as 看看 / 试试 / 说说.
    t = re.sub(r"(我|你|他|她|它|这|那|就|是|然后|那个|这个|的|了|我们|你们)\1+", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"^[，,。；;：:\s]+", "", t)
    t = re.sub(r"[，,；;：:\s]+$", "", t)
    return t

def is_noise(text, speech_s):
    """Short transients usually come out as a stray word; treat them as noise."""
    core = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
    if len(core) < 2:
        # one Chinese character can be a complete answer ("好", "行", "停")
        if re.fullmatch(r"[\u4e00-\u9fff]", core) and speech_s >= 0.3:
            return False
        return True
    if not re.search(r"[\u4e00-\u9fff]", text) and len(text.split()) == 1 and speech_s < 1.0:
        return True
    return False

def paste(text):
    if DRY:
        print("[dry]", text[:60], flush=True)
        return True
    try:
        subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=3)
        subprocess.run(["wtype", "-M", "shift", "-k", "insert", "-m", "shift"], timeout=5)
        return True
    except Exception as e:
        print("paste failed:", e, flush=True)
        return False

def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    set_state("idle")
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(MODEL_DIR, "model.int8.onnx"),
        tokens=os.path.join(MODEL_DIR, "tokens.txt"),
        num_threads=4, language="auto", use_itn=True)

    src = None
    if "--input" in sys.argv:
        src = sys.argv[sys.argv.index("--input") + 1]
    if src:
        class _F:
            def __init__(self, path):
                self.data = open(path, "rb").read(); self.i = 0; self.stdout = self
            def read(self, n):
                out = self.data[self.i:self.i + n]; self.i += len(out); return out
            def terminate(self): pass
        p = _F(src)
    else:
        dev = resolve_device()
        if not dev:
            print(f"bound microphone {DEVICE_MATCH!r} not connected; exiting", flush=True)
            return
        print(f"capturing from {dev}", flush=True)
        p = subprocess.Popen(["pw-record", "--target", dev, "--rate", str(SR),
                              "--channels", "1", "--format", "s16", "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def transcribe(buf):
        t = time.time()
        s = rec.create_stream()
        s.accept_waveform(SR, [x / 32768.0 for x in buf])
        rec.decode_stream(s)
        return (s.result.text or "").strip(), (time.time() - t) * 1000

    ring = deque()
    pre_frames = int(PRE_ROLL_S * SR / FRAME)
    end_frames = END_MS // 20
    seg = array.array("h")
    in_seg = False
    voiced_frames = silence = 0
    voiced_like = 0
    speech_frames = 0
    last_eval = 0
    interim = None
    log = open(LOG, "a", buffering=1)

    try:
        while True:
            chunk = p.stdout.read(FRAME * 2)
            if not chunk or len(chunk) < FRAME * 2:
                break
            fr = array.array("h"); fr.frombytes(chunk)
            voiced = peak_db(fr) >= SPEECH_DB
            ring.append((fr, voiced))
            while len(ring) > pre_frames + 2000:
                ring.popleft()

            if not in_seg:
                if voiced:
                    voiced_frames += 1
                    if voiced_frames >= 3:
                        in_seg = True
                        seg = array.array("h")
                        take = min(len(ring), pre_frames + voiced_frames)
                        for f, _ in list(ring)[-take:]:
                            seg.extend(f)
                        silence = 0
                        set_state("listening")
                else:
                    voiced_frames = 0
                continue

            seg.extend(fr)
            # Live verdict from acoustics only. Runs once a second on a 3 s tail:
            # cheap enough to stay below a few percent of one core.
            if speech_frames - last_eval >= 25 and speech_frames * 0.02 >= 0.5:
                last_eval = speech_frames
                tail = seg[-min(len(seg), 3 * SR):]
                if (peak_db(tail) >= SEG_PEAK_MIN and hf_ratio(tail) >= HF_MIN
                        and voiced_like / max(1, speech_frames) >= 0.2
                        and flatness(tail) < FLAT_MAX):
                    set_state("ok")

            if voiced:
                silence = 0
                speech_frames += 1
                if zcr(fr) < 0.25:
                    voiced_like += 1
            else:
                silence += 1

            dur = len(seg) / SR
            speech_s = dur - silence * 0.02

            # A short pause is enough to see whether the sentence already closed.
            if silence == EARLY_SILENCE_FRAMES and speech_s >= 2.0:
                text, ms = transcribe(seg)
                interim = text
                core = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
                looks_voiced = speech_frames > 0 and voiced_like / speech_frames >= 0.2
                bright = hf_ratio(seg) >= HF_MIN
                if (text and len(core) >= 6 and looks_voiced and bright and peak_db(seg) >= SEG_PEAK_MIN
                        and not is_noise(text, speech_s)
                        and text[-1] in END_PUNCT and (not ONLY_WINDOW or active_class() == ONLY_WINDOW)):
                    if paste(text):
                        log.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                                              "dur": round(dur, 2), "asr_ms": round(ms),
                                              "commit": "punctuation", "chars": len(text), "pasted": True,
                                              "peak": round(peak_db(seg), 1), "hf": round(hf_ratio(seg), 3),
                                              "voiced": round(voiced_like / speech_frames if speech_frames else 0.0, 2),
                                              "reason": None},
                                             ensure_ascii=False) + "\n")
                        in_seg = False; voiced_frames = silence = 0
                        seg = array.array("h"); interim = None
                        last_eval = 0
                        set_state("accepted")
                        continue

            if silence >= end_frames or dur >= MAX_SEG_S:
                ok = False
                if speech_s >= MIN_SPEECH_S:
                    text, ms = (interim, 0) if interim else transcribe(seg)
                    looks_voiced = speech_frames > 0 and voiced_like / speech_frames >= 0.2
                    bright = hf_ratio(seg) >= HF_MIN
                    vr = voiced_like / speech_frames if speech_frames else 0.0
                    tail = seg[-min(len(seg), 3 * SR):]     # same window as the live check
                    hf = hf_ratio(tail)
                    pk = peak_db(tail)
                    if not text:
                        reason = "empty"
                    elif not meaningful(text):
                        reason = "punct"
                    elif is_noise(text, speech_s):
                        reason = "noise"
                    elif vr < 0.2:
                        reason = "unvoiced"
                    elif hf < HF_MIN:
                        reason = "dull"
                    elif pk < SEG_PEAK_MIN:
                        reason = "quiet"
                    elif flatness(seg) >= FLAT_MAX:
                        reason = "flat"
                    elif ONLY_WINDOW and active_class() != ONLY_WINDOW:
                        reason = "window"
                    else:
                        reason = None
                    ok = paste(text) if reason is None else False
                    log.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                                          "dur": round(dur, 2), "asr_ms": round(ms),
                                          "commit": "silence", "chars": len(text), "pasted": ok,
                                          "peak": round(pk, 1), "hf": round(hf, 3),
                                          "voiced": round(vr, 2), "reason": reason},
                                         ensure_ascii=False) + "\n")
                in_seg = False; voiced_frames = silence = 0
                seg = array.array("h"); interim = None
                last_eval = 0
                set_state("accepted" if ok else "idle")
    finally:
        set_state("idle")
        p.terminate(); log.close()

if __name__ == "__main__":
    main()
