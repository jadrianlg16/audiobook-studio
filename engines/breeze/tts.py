"""Breeze TTS 2 adapter (BreezeBlue) — the *directable* engine.

Unlike every other engine here, Breeze takes a free-text performance note
("Speak slowly with a restrained, serious tone") alongside the text, and
understands inline vocal events like ``(sigh)``. That makes it the first engine
that can consume the AI director's output as prose instead of as a label picked
from a fixed vocabulary.

Three modes, one code path — which one you get depends on what you send:

===================  ==================  ===========  ==========================
mode                 reference clip      instruction  result
===================  ==================  ===========  ==========================
**Voice design**     no                  yes          invents a matching voice
**Voice clone**      yes + transcript    no           copies the speaker
**Voice direction**  yes + transcript    yes          same speaker, steered
===================  ==================  ===========  ==========================

One correction to that table, from the 2026-09-05 audit: "clone" is not a
separate server-side path. Upstream's ``instruction`` form field *defaults* to
"Speak clearly and naturally." and its template layer rejects an empty one, so
whenever a reference clip is present the server builds the **direction** prompt
regardless. Omitting the instruction gets you direction with a neutral note. The
genuinely instruction-free clone template exists upstream but is unreachable over
HTTP — it is only ever used as the CFG negative branch. The distinction is
invisible in the output for a neutral read, so the three-mode framing is still
the right way to *think* about it; it just isn't three code paths upstream.

⚠️ **Licensing.** The code here is ours, but Breeze TTS 2's *weights* are under
the BreezeBlue Research and Non-Commercial License, and that license explicitly
covers **self-hosted outputs**. Audio from this engine is fine for research and
evaluation — it is not licensed for a commercial audiobook. BreezeBlue's hosted
API is the commercial path. See ``engines/breeze/README.md``.

**Why a subprocess instead of importing the model.** BreezeBlue documents a CLI
and a local HTTP server (``python -m breeze_infer.api``) but no stable Python
API. Rather than guess at internals that can change under us, this adapter drives
the documented HTTP contract. ``BREEZE_SERVER_URL`` picks the mode:

- **unset → managed.** We spawn the server inside this container and own its
  lifecycle, so ``/unload`` genuinely returns the VRAM (we kill the process,
  which is more thorough than dropping torch references).
- **set → remote.** We only proxy. This is what lets a small box run the adapter
  against a Breeze server on a bigger machine — and what lets the test suite
  point the whole adapter at a stub with no GPU and no weights.
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
import numpy as np

from shared.engine_base import TTSEngine
from shared.schemas import Capabilities, EngineInfo, SynthRequest

# Inline vocal events, written exactly as they must appear in the text.
#
# These four — and *only* these four — are attested by the open-weight release
# (its README's Vocal Events highlight and its own CLI examples). Audited
# 2026-09-05: there is no event vocabulary anywhere else in the upstream repo. No
# tokenizer special tokens, no lookup table, no config list — a grep for
# "sigh|laugh|cough|clears throat" across /opt/breeze-tts hits README.md and
# nothing else. Vocal events are ordinary text tokens.
#
# This list used to carry 28 entries taken from BreezeBlue's *hosted* product
# docs. That was wrong twice over: the hosted product is a different model, and it
# writes English events in square brackets with different spellings ("[gasps]"),
# so it was not even evidence for the syntax. See docs/BREEZE-AUDIT-2026-09-05.md.
#
# Upstream says "such as", not "one of", so this is a defensible floor and not a
# proven ceiling. If a tag survives A/B testing on your checkpoint, add it:
#   BREEZE_AUDIO_TAGS="(sigh),(laugh),(clears throat),(cough),(gasp)"
#
# Chinese uses square brackets instead — [笑] [咳嗽] [清嗓子] [叹气] — so a Chinese
# book needs BREEZE_AUDIO_TAGS set to those. The director is only ever offered
# what this list advertises.
_DEFAULT_AUDIO_TAGS = ["(laugh)", "(cough)", "(clears throat)", "(sigh)"]

# cfg_scale is Breeze's instruction-adherence knob. Upstream's default is 1.0 and
# its examples pass 4 to strengthen instruction-following; it validates only that
# the value is finite and > 0. We expose it through the shared 0..1 ``emotion``
# slider, so 0.5 gives 4.5 — *near* their recommended 4, not equal to it.
#
# The floor of 1.0 is deliberate: the fast path's warmup profile refuses cfg < 1.0
# (models/warmup_profile.py), so 1.0 is the safer floor of the two constraints.
# Non-round values are safe even with --fast-all: CUDA graphs bucket by CFG
# *shape* (no_cfg / single_cfg), and guidance_scale is documented as
# "runtime-mutable without recapture" (models/cudagraph/backbone_graph.py).
#
# This is a bridge between two different ideas (expressiveness vs. adherence),
# not a native mapping — override per request via extra.cfg_scale.
_CFG_MIN, _CFG_MAX = 1.0, 8.0


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


class BreezeEngine(TTSEngine):
    SR = 24_000  # Breeze streams mono 24 kHz signed 16-bit little-endian PCM.

    def __init__(self) -> None:
        super().__init__()
        self._proc: subprocess.Popen | None = None
        self._base_url: str = ""

    # ---- metadata ----------------------------------------------------------

    def info(self) -> EngineInfo:
        tags_raw = os.environ.get("BREEZE_AUDIO_TAGS", "").strip()
        tags = (
            [t.strip() for t in tags_raw.split(",") if t.strip()]
            if tags_raw
            else list(_DEFAULT_AUDIO_TAGS)
        )
        return EngineInfo(
            id="breeze",
            name="Breeze TTS 2",
            description=(
                "Takes a written performance note ('slowly, restrained') plus inline "
                "vocal events like (sigh). Clone a voice and direct it, or design one "
                "from a description alone. Research/non-commercial weights."
            ),
            license="BreezeBlue Research & Non-Commercial (weights)",
            homepage="https://github.com/breezeblue-ai/breeze-tts",
            sample_rate=self.SR,
            capabilities=Capabilities(
                cloning=True,
                emotion_tags=True,
                emotion_scalar=True,
                instruction=True,
                requires_gpu=True,
                languages=["en", "zh"],
            ),
            audio_tags=tags,
        )

    # ---- lifecycle ---------------------------------------------------------

    def _load(self) -> None:
        external = os.environ.get("BREEZE_SERVER_URL", "").strip()
        if external:
            # Remote mode: someone else owns the server; just check we can reach it.
            self._base_url = external.rstrip("/")
            self._wait_for_port(*self._host_port(self._base_url))
        else:
            self._base_url = self._spawn_server()

        if _env_flag("BREEZE_WARMUP", True):
            # A real inference, not a port check: a reachable server that cannot
            # actually synthesize (missing weights, wrong CUDA) should fail here,
            # at load time, rather than midway through a book.
            self._smoke_test()

    def _spawn_server(self) -> str:
        model_dir = os.environ.get("BREEZE_MODEL_DIR", "/models/breeze-tts-2")
        repo_dir = os.environ.get("BREEZE_REPO_DIR", "/opt/breeze-tts")
        host = os.environ.get("BREEZE_HOST", "127.0.0.1")
        port = int(os.environ.get("BREEZE_PORT", "7860"))

        if not os.path.isdir(model_dir):
            raise RuntimeError(
                f"Breeze checkpoint not found at {model_dir}. Download it once on the "
                f"host and bind-mount it — see engines/breeze/README.md."
            )

        cmd = [
            sys.executable, "-m", "breeze_infer.api", model_dir,
            "--host", host, "--port", str(port),
        ]
        if _env_flag("BREEZE_FAST"):
            # ~2x faster but roughly doubles VRAM (BreezeBlue: 7.7 GiB -> 14.4 GiB).
            cmd.append("--fast-all")

        self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            self._wait_for_port(host, port)
        except Exception as exc:
            # Surface the server's own output — "CUDA out of memory" is far more
            # useful than "timed out waiting for 127.0.0.1:7860".
            raise RuntimeError(
                f"breeze_infer.api did not come up ({exc}). "
                f"Last output:\n{self._drain_proc_output()}"
            ) from exc
        return f"http://{host}:{port}"

    def _wait_for_port(self, host: str, port: int) -> None:
        # Loading a 3B checkpoint onto a GPU is slow, and a cold container may also
        # be paging weights off disk — default generously.
        deadline = time.monotonic() + _env_float("BREEZE_STARTUP_TIMEOUT", 600.0)
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"breeze_infer.api exited with code {self._proc.returncode}:\n"
                    f"{self._drain_proc_output()}"
                )
            try:
                with socket.create_connection((host, port), timeout=2.0):
                    return
            except OSError:
                time.sleep(1.0)
        raise TimeoutError(f"timed out waiting for Breeze server at {host}:{port}")

    def _drain_proc_output(self, limit: int = 2000) -> str:
        if self._proc is None or self._proc.stdout is None:
            return "(no output captured)"
        try:
            # Only safe to read once the process is dead; on a live pipe this
            # would block until the server exits.
            out = self._proc.stdout.read() if self._proc.poll() is not None else ""
        except Exception as exc:  # noqa: BLE001
            out = f"(could not read output: {exc})"
        return (out or "(no output captured)")[-limit:]

    def _smoke_test(self) -> None:
        audio = self._request(
            text="Ready.",
            instruction="A calm narrator.",
            cfg_scale=4.0,
            ref_audio=None,
            ref_text=None,
            seed=0,
            timeout=_env_float("BREEZE_STARTUP_TIMEOUT", 600.0),
        )
        if audio.size == 0:
            raise RuntimeError("Breeze warm-up returned no audio")

    def _unload(self) -> None:
        proc, self._proc = self._proc, None
        self._base_url = ""
        if proc is None or proc.poll() is not None:
            return
        # Killing the process is what actually frees the VRAM in managed mode —
        # there are no torch objects in *this* process to drop.
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    # ---- synthesis ---------------------------------------------------------

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        instruction = req.instruction or req.emotion_label or None

        ref_audio = (
            base64.b64decode(req.reference_audio_b64) if req.reference_audio_b64 else None
        )
        ref_text = (req.reference_text or "").strip() or None
        if ref_audio is None and ref_text:
            # Upstream's pairing rule is symmetric: it 400s when
            # ``has_reference != bool(ref_text)``, so a transcript with no clip is
            # rejected just as hard as a clip with no transcript. A caller that
            # sets reference_text and forgets the audio is asking for voice
            # *design*, and the transcript is simply not part of that request —
            # dropping it is what they meant. Sending it would turn a working
            # request into an upstream 400 surfaced as our 500.
            ref_text = None
        if ref_audio is not None and not ref_text:
            # Breeze conditions on the transcript, not just the audio. Without it
            # the clone degrades badly, so refuse loudly instead of sounding bad.
            raise ValueError(
                "Breeze voice cloning needs the reference clip's exact transcript "
                "(SynthRequest.reference_text)"
            )
        if ref_audio is None and not instruction:
            raise ValueError(
                "Breeze needs either a reference clip (+ transcript) to clone, or an "
                "instruction describing the voice to design"
            )

        cfg = req.extra.get("cfg_scale")
        if cfg is None:
            emotion = 0.5 if req.emotion is None else req.emotion
            cfg = _CFG_MIN + emotion * (_CFG_MAX - _CFG_MIN)
        cfg = max(1.0, min(10.0, float(cfg)))

        # NOTE: Breeze's documented server takes no speed/rate parameter, so
        # req.speed is deliberately ignored (the shared contract lets an engine
        # ignore what it doesn't support). Ask for pace in the instruction
        # instead — "speak slowly" is exactly what this model is built for.
        return self._request(
            text=req.text,
            instruction=instruction,
            cfg_scale=cfg,
            ref_audio=ref_audio,
            ref_text=ref_text,
            seed=req.seed,
        )

    def _request(
        self,
        *,
        text: str,
        instruction: str | None,
        cfg_scale: float,
        ref_audio: bytes | None,
        ref_text: str | None,
        seed: int | None,
        timeout: float | None = None,
    ) -> np.ndarray:
        data: dict[str, str] = {"text": text, "cfg_scale": str(cfg_scale)}
        if instruction:
            data["instruction"] = instruction
        if ref_text:
            data["ref_text"] = ref_text
        if seed is not None:
            data["seed"] = str(seed)

        # Every field goes through ``files`` as a (None, value) part so the request
        # is *always* multipart/form-data — which is what BreezeBlue documents
        # (their examples are all ``curl -F``). Passing these via ``data=`` instead
        # would silently fall back to urlencoded whenever there is no reference
        # clip, and a server reading multipart would then see no fields at all.
        parts: list[tuple[str, tuple]] = [(k, (None, v)) for k, v in data.items()]
        if ref_audio:
            parts.append(("ref_audio", ("reference.wav", ref_audio, "audio/wav")))

        url = f"{self._base_url}/v1/audio/speech"
        with httpx.Client(timeout=timeout or _env_float("BREEZE_REQUEST_TIMEOUT", 900.0)) as c:
            resp = c.post(url, files=parts)
            if resp.status_code == 409:
                # Upstream serialises on a module-level lock and 409s a second
                # in-flight request rather than queueing. Worth naming, because a
                # bake-off fans out across engines and this is the one that can't
                # take it.
                raise RuntimeError(
                    "Breeze server is already synthesizing (HTTP 409). The upstream "
                    "API is single-concurrency by design — serialize calls to it, or "
                    "run one server per concurrent request."
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Breeze server HTTP {resp.status_code}: {resp.text[:300]}")
            return self._pcm_to_float32(resp.content)

    @staticmethod
    def _pcm_to_float32(raw: bytes) -> np.ndarray:
        """Breeze streams raw signed 16-bit little-endian mono PCM (no WAV header)."""
        if not raw:
            return np.zeros(0, dtype=np.float32)
        if len(raw) % 2:  # a truncated stream would misalign every sample after it
            raw = raw[:-1]
        pcm = np.frombuffer(raw, dtype="<i2")
        return (pcm.astype(np.float32) / 32768.0).copy()

    @staticmethod
    def _host_port(url: str) -> tuple[str, int]:
        parts = urlsplit(url)
        default_port = 443 if parts.scheme == "https" else 80
        return parts.hostname or "127.0.0.1", parts.port or default_port
