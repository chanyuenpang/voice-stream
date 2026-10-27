
#!/usr/bin/env python3
"""enroll.py: build a voiceprint from a WAV file (16 kHz mono s16).

    enroll.py /path/to/sample.wav
"""
import array, json, math, os, sys
import sherpa_onnx

APP_DIR = os.path.expanduser(os.environ.get("VOICE_STREAM_HOME", "~/.local/share/voice-stream"))
MODEL = os.path.expanduser(os.environ.get("VOICE_STREAM_SPEAKER_MODEL", APP_DIR + "/speaker.onnx"))
OUT = os.path.expanduser(os.environ.get("VOICE_STREAM_VOICEPRINT", APP_DIR + "/voiceprint.json"))
SR = 16000
FRAME = 320

def read_wav(path):
    data = open(path, "rb").read()
    i = data.find(b"data")
    raw = data[i + 8:] if i > 0 else data
    a = array.array("h"); a.frombytes(raw[: len(raw) // 2 * 2])
    return a

def chunks(a):
    """Split on 300 ms of near-silence; keep pieces with real speech."""
    out, cur, quiet = [], [], 0
    for i in range(0, len(a) - FRAME, FRAME):
        fr = a[i:i + FRAME]
        p = max((abs(x) for x in fr), default=0) / 32768
        db = 20 * math.log10(p) if p > 0 else -120
        if db < -45:
            quiet += 1
            if quiet >= 15 and cur:
                out.append(cur); cur, quiet = [], 0
        else:
            quiet = 0; cur.extend(fr)
    if cur: out.append(cur)
    return [c for c in out if len(c) / SR >= 0.5]

def main():
    path = sys.argv[1]
    a = read_wav(path)
    parts = chunks(a)
    if not parts:
        print("no speech found"); return
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=2)
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    embs = []
    for c in parts:
        s = ext.create_stream()
        s.accept_waveform(SR, [x / 32768.0 for x in c])
        while not ext.is_ready(s):
            ext.compute(s)
        embs.append(ext.compute(s))
    dim = len(embs[0])
    avg = [sum(e[i] for e in embs) / len(embs) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in avg)) or 1.0
    avg = [v / norm for v in avg]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"dim": dim, "embedding": avg, "chunks": len(parts)}, open(OUT, "w"))
    print(f"enrolled from {len(parts)} chunks, dim={dim}, saved {OUT}")

if __name__ == "__main__":
    main()
