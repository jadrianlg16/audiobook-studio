#!/usr/bin/env python
"""Run the AI director against a real LLM and show what survived validation.

This is the loop for tuning ``gateway/app/prompts/director.md``: edit the prompt,
re-run this, read the diff. It needs no gateway, no engines and no GPU — just
Ollama (or a Gemini key), so you can iterate on direction quality on any machine
and only spend GPU time once the prose is good.

The output is split deliberately:

  KEPT      what the engine will actually receive
  REJECT    what the model produced, why it was thrown away, and the book's own
            words next to it — the diff you tune the prompt against
  NONE      the model answered but added no tags (a valid choice, not a failure)
  IGNORE    the model answered for an engine that can't use it

A high REJECT count is a prompt bug, not a model bug — that is the signal you
are tuning against. It means the model paraphrased the passage instead of just
annotating it.

Examples
--------
    python scripts/try_director.py --model gemma3:12b
    python scripts/try_director.py --model qwen3:30b --text-file pipeline/samples/dialogue.txt
    python scripts/try_director.py --model gemma3:12b --gateway http://localhost:8080
    python scripts/try_director.py --model gemma3:12b --repeat 3   # consistency check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A Windows console defaults to cp1252, and directed text is full of curly quotes,
# em dashes and (on Chinese passages) CJK. Without this the script dies printing
# the model's own output — on Windows only, which is exactly the bug you don't
# want to find on the other machine.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # already UTF-8, or not a real tty
        pass

from gateway.app.models import DirectorEngineSpec, DirectorRequest  # noqa: E402
from gateway.app.services import director as d  # noqa: E402

# Stand-in specs so the script runs with nothing else up. Pass --gateway to use
# the real ones from a running stack instead.
_FALLBACK_SPECS = [
    DirectorEngineSpec(
        id="breeze",
        name="Breeze TTS 2",
        instruction=True,
        # Only the four events the open weights attest — see
        # docs/BREEZE-AUDIT-2026-09-05.md. --gateway fetches the live list instead.
        audio_tags=["(laugh)", "(cough)", "(clears throat)", "(sigh)"],
        emotion_scalar=True,
        emotion_tags=True,
    ),
    DirectorEngineSpec(
        id="kokoro",
        name="Kokoro-82M",
        voices=["af_heart", "af_bella", "am_michael", "bf_emma", "bm_george"],
    ),
    DirectorEngineSpec(
        id="indextts2",
        name="IndexTTS 2",
        emotion_labels=["neutral", "happy", "sad", "angry", "afraid", "surprised", "calm"],
        emotion_scalar=True,
        emotion_tags=True,
    ),
]

_SAMPLE = (
    "She put the letter down and did not pick it up again. Outside, the rain had "
    "stopped, and the street was very quiet. It was over, and she had known for weeks."
)


def _specs_from_gateway(url: str) -> list[DirectorEngineSpec]:
    import httpx

    resp = httpx.get(f"{url.rstrip('/')}/api/engines", timeout=15.0)
    resp.raise_for_status()
    specs = []
    for e in resp.json():
        info = e.get("info")
        if not e.get("available") or not info:
            continue
        caps = info["capabilities"]
        specs.append(
            DirectorEngineSpec(
                id=info["id"],
                name=info["name"],
                emotion_labels=info.get("emotion_labels", []),
                voices=[v["id"] for v in info.get("voices", [])],
                audio_tags=info.get("audio_tags", []),
                emotion_scalar=caps.get("emotion_scalar", False),
                emotion_tags=caps.get("emotion_tags", False),
                instruction=caps.get("instruction", False),
            )
        )
    if not specs:
        raise SystemExit(f"no available engines at {url} — is the stack up?")
    return specs


async def _one_run(req: DirectorRequest, ollama_url: str, gemini_key: str) -> tuple[dict, list]:
    """Return (raw parsed reply, validated per_engine) so we can show the delta."""
    prompt = d.build_prompt(req)
    if req.provider == "ollama":
        reply = await d._ollama(req.model, prompt, req.ollama_url or ollama_url)
    else:
        key = req.api_key or gemini_key
        if not key:
            raise SystemExit("--api-key (or GEMINI_API_KEY) is required for gemini")
        reply = await d._gemini(req.model, prompt, key)
    raw = d._parse(reply)
    kept = d._validate_per_engine(raw.get("per_engine"), req.engines, raw.get("scalar", 0.5), req.text)
    return raw, kept


def _report(raw: dict, kept: list, specs: list[DirectorEngineSpec], source: str) -> tuple[int, int]:
    """Print one run. Returns (rejected, ignored).

    The two are very different signals. *ignored* means the model answered for an
    engine that never asked — untidy, but harmless, and validation drops it.
    *rejected* means the model returned an annotation that failed the word-identity
    check: it tried to change the book. That is the number to drive to zero.
    """
    by_id = {s.id: s for s in specs}
    raw_by_id = {
        item.get("engine_id"): item
        for item in (raw.get("per_engine") or [])
        if isinstance(item, dict)
    }
    rejected = ignored = 0

    pace = raw.get("pace")
    print(f"\n  summary        {raw.get('summary', '')}")
    print(f"  emotion        {raw.get('emotion')}   scalar {raw.get('scalar')}")
    print(f"  voice_hint     {raw.get('voice_hint', '')}")
    # The two engine-agnostic outputs. `scene_note` is for the human and must
    # never appear as an engine instruction below; `pace` is the one direction
    # every engine can honour, so it is what a Kokoro book actually gets.
    print(f"  scene_note     {raw.get('scene_note', '') or '(none)'}")
    print(f"  pace           {pace if pace is not None else '(none)'}   -> render.speed on engines with instruction = no")
    print(f"  segments       {len(raw.get('segments') or [])}")

    for entry in kept:
        eid = entry["engine_id"]
        spec = by_id.get(eid)
        before = raw_by_id.get(eid, {})
        print(f"\n  ── {eid} " + "─" * (56 - len(eid)))
        print(f"     scalar        {entry['scalar']:.2f}")
        if entry["emotion_label"] or entry["voice"]:
            print(f"     emotion/voice {entry['emotion_label']} / {entry['voice']}")

        if spec and spec.instruction:
            if entry["instruction"]:
                print(f"     KEPT    instruction  {entry['instruction']}")
            elif before.get("instruction"):
                rejected += 1
                print(f"     REJECT  instruction  {str(before['instruction'])[:100]}")
        elif before.get("instruction"):
            ignored += 1
            print("     IGNORE  instruction  (engine declared instruction = no)")

        if spec and spec.audio_tags:
            # text_status is the gateway's own verdict, so this no longer has to
            # infer "it must have been rejected" from a missing field. The
            # distinction it adds is real: a model that returns the passage
            # *unchanged* did not rewrite the book, and counting that as REJECTED
            # (which this script used to do) inflates the one number you tune
            # against.
            status = entry.get("text_status") or ("kept" if entry["text"] else "none")
            if status == "kept" and entry["text"]:
                tags = [t for t in spec.audio_tags if t in entry["text"]]
                print(f"     KEPT    tags         {', '.join(tags) or '(none matched)'}")
                print(f"             text         {entry['text'][:160]}")
            elif status == "rejected":
                rejected += 1
                reason = entry.get("text_reason") or "not the source passage"
                print(f"     REJECT  text         {reason}")
                # The model's own words, canonicalized — this is the diff you
                # actually tune the prompt against.
                got = entry.get("rejected_text") or before.get("text") or ""
                print(f"             got          {str(got)[:160]}")
                print(f"             book         {source[:160]}")
            elif before.get("text"):
                print("     NONE    text         returned the passage with no tags added")
        elif before.get("text"):
            ignored += 1
            print("     IGNORE  text         (engine declared audio tags = none)")

    return rejected, ignored


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=os.environ.get("DIRECTOR_MODEL", "gemma3:12b"))
    p.add_argument("--provider", choices=["ollama", "gemini"], default="ollama")
    p.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    p.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    p.add_argument("--text", default=None)
    p.add_argument("--text-file", type=Path, default=None)
    p.add_argument("--gateway", default=None, help="fetch real engine specs from a running gateway")
    p.add_argument("--repeat", type=int, default=1, help="run N times to gauge consistency")
    p.add_argument("--json", action="store_true", help="dump the raw model reply too")
    args = p.parse_args()

    text = args.text or (args.text_file.read_text(encoding="utf-8") if args.text_file else _SAMPLE)
    text = text.strip()
    specs = _specs_from_gateway(args.gateway) if args.gateway else list(_FALLBACK_SPECS)

    print(f"director   {args.provider}:{args.model}")
    print(f"prompt     {d._prompt_path()}  (version {d.prompt_version()})")
    print(f"engines    {', '.join(s.id for s in specs)}")
    print(f"passage    {len(text)} chars")

    total_rejected = total_ignored = 0
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n{'=' * 66}\nrun {i + 1}/{args.repeat}")
        req = DirectorRequest(
            text=text,
            provider=args.provider,
            model=args.model,
            api_key=args.api_key or None,
            ollama_url=args.ollama_url,
            engines=specs,
        )
        try:
            raw, kept = asyncio.run(_one_run(req, args.ollama_url, args.api_key))
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
            print(f"\n  FAILED  {type(exc).__name__}: {exc}")
            sys.exit(1)
        rejected, ignored = _report(raw, kept, specs, text)
        total_rejected += rejected
        total_ignored += ignored
        if args.json:
            print("\n  raw reply:\n" + json.dumps(raw, indent=2, ensure_ascii=False))

    print(f"\n{'=' * 66}")
    print(f"rejected (model changed the passage): {total_rejected}   <- drive this to 0")
    print(f"ignored  (answered for an engine that can't use it): {total_ignored}")
    if total_rejected:
        print("\n-> tune gateway/app/prompts/director.md and re-run. If a bigger model")
        print("   fixes it and a smaller one can't, that is your director floor.")
    sys.exit(1 if total_rejected else 0)


if __name__ == "__main__":
    main()
