"""Breeze adapter tests — real HTTP round trips, no GPU and no weights.

A stub stands in for ``breeze_infer.api``: it speaks the same documented contract
(``POST /v1/audio/speech``, multipart in, raw 24 kHz s16le PCM out) and records
what it was sent. Pointing ``BREEZE_SERVER_URL`` at it drives the *real* adapter
through the *real* engine app, so these tests cover the parts that actually break
in production — multipart encoding, PCM framing, and the guard rails — on any
machine, including one with no NVIDIA card.

Run from the project root:  pytest engines/breeze/tests
"""

from __future__ import annotations

import email
import io
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
from fastapi.testclient import TestClient

from engines.breeze.tts import BreezeEngine
from shared.engine_base import create_engine_app
from shared.schemas import SynthRequest

# 0.25 s of 24 kHz audio, as the real server would stream it: raw signed 16-bit
# little-endian mono PCM with no WAV header.
_PCM_SAMPLES = 6000
_STUB_PCM = (np.zeros(_PCM_SAMPLES, dtype="<i2") + 1000).tobytes()


class _Recorder:
    """Last request the stub saw, shared with the test body."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.calls = 0


def _make_handler(rec: _Recorder, status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # keep pytest output clean
            pass

        def do_POST(self) -> None:
            assert self.path == "/v1/audio/speech", self.path
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            rec.calls += 1
            rec.fields, rec.files = _parse_multipart(self.headers.get("Content-Type", ""), body)
            if status >= 400:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"stub failure")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(_STUB_PCM)))
            self.end_headers()
            self.wfile.write(_STUB_PCM)

    return Handler


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, bytes]]:
    raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    msg = email.message_from_bytes(raw)
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in msg.get_payload() if msg.is_multipart() else []:
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if part.get_param("filename", header="content-disposition"):
            files[str(name)] = payload
        else:
            fields[str(name)] = payload.decode("utf-8")
    return fields, files


@pytest.fixture
def stub(monkeypatch):
    """A running Breeze stub; yields the recorder. Adapter env is pointed at it."""
    rec = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(rec))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    monkeypatch.setenv("BREEZE_SERVER_URL", f"http://{host}:{port}")
    monkeypatch.setenv("LAZY_LOAD", "1")
    monkeypatch.setenv("BREEZE_STARTUP_TIMEOUT", "10")
    try:
        yield rec
    finally:
        server.shutdown()
        server.server_close()


def _client(stub_rec: _Recorder) -> TestClient:
    return TestClient(create_engine_app(BreezeEngine()))


# ---- contract ---------------------------------------------------------------

def test_info_declares_direction_and_tags() -> None:
    info = BreezeEngine().info()
    assert info.capabilities.instruction is True
    assert info.capabilities.cloning is True
    assert info.sample_rate == 24_000
    # The director may only insert tags the engine actually advertises.
    assert "(sigh)" in info.audio_tags


def test_default_tags_are_only_the_four_upstream_attests() -> None:
    """Audit 2026-09-05: the open weights document exactly these four events and
    carry no event vocabulary anywhere else. The list previously held 28 entries
    taken from BreezeBlue's *hosted* docs — a different model, which even writes
    English events in square brackets. Advertising a tag the checkpoint never saw
    means the director inserts it and the model reads it aloud as words.

    Widen this only via BREEZE_AUDIO_TAGS, after A/B-ing on a real checkpoint.
    """
    assert BreezeEngine().info().audio_tags == [
        "(laugh)",
        "(cough)",
        "(clears throat)",
        "(sigh)",
    ]


def test_default_tags_use_the_english_parenthesis_syntax() -> None:
    """Parentheses in English, square brackets in Chinese. A tag in the wrong
    bracket style is not a formatting nit — it is a different token sequence."""
    for tag in BreezeEngine().info().audio_tags:
        assert tag.startswith("(") and tag.endswith(")"), tag
        assert tag == tag.lower(), tag


def test_audio_tags_are_overridable(monkeypatch) -> None:
    """You pin the tags that survived A/B testing on your own checkpoint."""
    monkeypatch.setenv("BREEZE_AUDIO_TAGS", "(sigh), (laugh)")
    assert BreezeEngine().info().audio_tags == ["(sigh)", "(laugh)"]


# ---- voice design / direction ----------------------------------------------

def test_voice_design_sends_instruction_and_returns_wav(stub) -> None:
    client = _client(stub)
    resp = client.post(
        "/synthesize",
        json={"text": "The house was quiet.", "instruction": "Slow and hushed."},
    )
    assert resp.status_code == 200, resp.text

    with wave.open(io.BytesIO(resp.content), "rb") as w:
        assert w.getframerate() == 24_000
        assert w.getnchannels() == 1
        assert w.getnframes() == _PCM_SAMPLES  # every sample survived the round trip

    assert stub.fields["text"] == "The house was quiet."
    assert stub.fields["instruction"] == "Slow and hushed."
    assert "ref_audio" not in stub.files  # design mode sends no reference


def test_emotion_scalar_maps_onto_cfg_scale(stub) -> None:
    client = _client(stub)
    client.post("/synthesize", json={"text": "hi", "instruction": "calm", "emotion": 0.5})
    # Over the 1..8 span, 0.5 gives 4.5 — *near* BreezeBlue's recommended 4, not
    # equal to it. (The old comment here claimed it landed on 4 exactly.) Any
    # value is safe on the fast path: graphs bucket by CFG shape, and upstream
    # documents guidance_scale as runtime-mutable without recapture.
    assert float(stub.fields["cfg_scale"]) == pytest.approx(4.5)

    client.post("/synthesize", json={"text": "hi", "instruction": "calm", "emotion": 0.0})
    assert float(stub.fields["cfg_scale"]) == pytest.approx(1.0)


def test_extra_cfg_scale_overrides_the_slider(stub) -> None:
    client = _client(stub)
    client.post(
        "/synthesize",
        json={"text": "hi", "instruction": "calm", "emotion": 0.9, "extra": {"cfg_scale": 3}},
    )
    assert float(stub.fields["cfg_scale"]) == pytest.approx(3.0)


def test_emotion_label_is_used_when_no_instruction_given(stub) -> None:
    """Keeps the engine usable from the existing preset UI, which only has labels."""
    client = _client(stub)
    client.post("/synthesize", json={"text": "hi", "emotion_label": "afraid"})
    assert stub.fields["instruction"] == "afraid"


# ---- cloning guard rails ----------------------------------------------------

def test_clone_sends_reference_audio_and_transcript(stub) -> None:
    import base64

    client = _client(stub)
    resp = client.post(
        "/synthesize",
        json={
            "text": "Hello again.",
            "reference_audio_b64": base64.b64encode(b"RIFFfake-wav").decode(),
            "reference_text": "This is the reference transcript.",
            "instruction": "Warmer this time.",
        },
    )
    assert resp.status_code == 200, resp.text
    assert stub.files["ref_audio"] == b"RIFFfake-wav"
    assert stub.fields["ref_text"] == "This is the reference transcript."


def test_clone_without_transcript_is_a_400(stub) -> None:
    """Breeze conditions on the transcript. Silently cloning without it produces
    a bad voice, so this must fail loudly as caller error — not a 500."""
    import base64

    client = _client(stub)
    resp = client.post(
        "/synthesize",
        json={
            "text": "Hello again.",
            "reference_audio_b64": base64.b64encode(b"RIFFfake-wav").decode(),
        },
    )
    assert resp.status_code == 400
    assert "transcript" in resp.text


def test_reference_text_without_a_clip_is_dropped_not_sent(stub) -> None:
    """Upstream's pairing rule is symmetric — it 400s when ref_audio and ref_text
    disagree in *either* direction. A transcript with no clip is a voice-design
    request with a stray field, so the adapter drops it. Sending it anyway turned
    a working synthesis into an upstream 400 surfaced as our 500."""
    client = _client(stub)
    resp = client.post(
        "/synthesize",
        json={
            "text": "The house was quiet.",
            "instruction": "Slow and hushed.",
            "reference_text": "A transcript with no audio to go with it.",
        },
    )
    assert resp.status_code == 200, resp.text
    assert "ref_text" not in stub.fields
    assert "ref_audio" not in stub.files


def test_no_reference_and_no_instruction_is_a_400(stub) -> None:
    client = _client(stub)
    resp = client.post("/synthesize", json={"text": "Hello."})
    assert resp.status_code == 400
    assert "instruction" in resp.text


def test_upstream_error_surfaces_as_500_not_a_crash(monkeypatch) -> None:
    rec = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(rec, status=503))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("BREEZE_SERVER_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("LAZY_LOAD", "1")
    monkeypatch.setenv("BREEZE_WARMUP", "0")  # let the failure land on the real call
    try:
        client = TestClient(create_engine_app(BreezeEngine()))
        resp = client.post("/synthesize", json={"text": "hi", "instruction": "calm"})
        assert resp.status_code == 500
        assert "503" in resp.text
    finally:
        server.shutdown()
        server.server_close()


def test_warmup_runs_a_real_inference_before_serving(stub) -> None:
    """A reachable server that can't synthesize must fail at load, not mid-book."""
    client = _client(stub)
    client.get("/health")  # lazy: not loaded yet
    before = stub.calls
    client.post("/synthesize", json={"text": "hi", "instruction": "calm"})
    # First synth == warm-up inference + the real one.
    assert stub.calls - before == 2


# ---- PCM framing ------------------------------------------------------------

def test_pcm_decode_is_signed16_little_endian() -> None:
    raw = np.array([0, 32767, -32768, 1000], dtype="<i2").tobytes()
    out = BreezeEngine._pcm_to_float32(raw)
    assert out.dtype == np.float32
    assert out[0] == pytest.approx(0.0)
    assert out[1] == pytest.approx(32767 / 32768.0, abs=1e-6)
    assert out[2] == pytest.approx(-1.0)


def test_pcm_decode_tolerates_a_truncated_stream() -> None:
    """A stream cut mid-sample must drop the stray byte, not shift every sample
    after it by one byte (which would render as loud noise)."""
    raw = np.array([100, 200], dtype="<i2").tobytes() + b"\x7f"
    out = BreezeEngine._pcm_to_float32(raw)
    assert len(out) == 2
    assert BreezeEngine._pcm_to_float32(b"").size == 0
