#!/usr/bin/env python
"""A stand-in for `breeze_infer.api` — same contract, no GPU, no weights.

Breeze TTS 2 needs ~7.7 GiB of VRAM. This speaks the same documented HTTP
contract (``POST /v1/audio/speech``, multipart in, raw 24 kHz signed 16-bit
little-endian mono PCM out) and answers with a tone, so you can develop and
exercise the *whole* stack — director, gateway, bake-off UI, pipeline — on a
laptop or an 8 GB card, and only spend real GPU time once the plumbing is right.

It also prints every request, which makes it the fastest way to see exactly what
instruction and tagged text an engine is actually receiving.

    python scripts/breeze_stub.py --port 7860

Then point the adapter at it:

    BREEZE_SERVER_URL=http://127.0.0.1:7860

The audio is a plain tone, not speech. It proves the plumbing, never the voice.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys

try:
    import uvicorn
    from fastapi import FastAPI, Form, Request, UploadFile
    from fastapi.responses import Response
except ImportError:  # pragma: no cover
    sys.exit("needs fastapi + uvicorn: pip install fastapi uvicorn python-multipart")

SR = 24_000
app = FastAPI(title="Breeze TTS 2 stub")


def _tone(seconds: float, hz: float = 220.0) -> bytes:
    """Signed 16-bit little-endian mono PCM — the exact framing Breeze streams."""
    n = max(1, int(SR * seconds))
    # A short fade at each end keeps concatenated clips from clicking.
    fade = min(n // 10, SR // 50) or 1
    out = bytearray()
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        out += struct.pack("<h", int(0.25 * env * 32767 * math.sin(2 * math.pi * hz * i / SR)))
    return bytes(out)


@app.post("/v1/audio/speech")
async def speech(
    request: Request,
    text: str = Form(...),
    cfg_scale: float = Form(4.0),
    instruction: str | None = Form(None),
    ref_text: str | None = Form(None),
    seed: int | None = Form(None),
    ref_audio: UploadFile | None = None,
) -> Response:
    clip = await ref_audio.read() if ref_audio is not None else b""
    mode = "direction" if (clip and instruction) else "clone" if clip else "design"

    print(f"\n[breeze-stub] {mode}  cfg_scale={cfg_scale}  seed={seed}")
    print(f"  text        {text[:160]}")
    if instruction:
        print(f"  instruction {instruction[:160]}")
    if ref_text:
        print(f"  ref_text    {ref_text[:80]}  ({len(clip)} bytes of audio)")
    sys.stdout.flush()

    # Roughly audiobook pace, so timings downstream are not absurd.
    seconds = max(0.4, len(text.split()) / 2.6)
    # Pitch shifts with cfg_scale purely so a bake-off's clips are distinguishable
    # by ear while you are wiring things up.
    return Response(_tone(seconds, hz=180.0 + 12.0 * cfg_scale), media_type="application/octet-stream")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "note": "stub — returns a tone, not speech"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7860)
    args = p.parse_args()
    print(f"breeze stub on http://{args.host}:{args.port}  (POST /v1/audio/speech)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
