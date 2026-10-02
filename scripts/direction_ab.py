#!/usr/bin/env python
"""Blind A/B test: does a director's written note make a passage sound better?

This is the experiment that decides whether direction is worth more work: render
the same passages with and without the LLM director's note on an engine that
takes one, strip every clue about which take is which, and let a person choose.

Conditions, per passage (only ``render.instruction`` differs between them):

- **A** plain: no note.
- **B** directed: the note the book-scale director pass (``pipeline.direct``)
  stamps on the passage's lines — the product path, not a special prompt.
- **C** one word: the director's one-word ``emotion`` for the scene as the note.

Part 1 of the listening page is A vs B for every passage; part 2 (optional) is
B vs C — "is a written note worth more than a label?".

Everything goes through the running gateway: the director (``/api/director``)
and synthesis (``/api/synthesize`` via ``render.http_synth``). The engine seeds
each take from the text and voice, not the note, so the three takes of a line
start from the same random state and differ by the note alone.

Steps (each is resumable; finished passages are skipped on a rerun):

    python scripts/direction_ab.py prepare --engine qwen3tts --voice <id> --model gemma3:12b
    python scripts/direction_ab.py render
    python scripts/direction_ab.py serve            # then open the printed URL and listen
    python scripts/direction_ab.py score

Files live in ``output/direction-ab/`` (git-ignored). The listening page serves
only ``listen/``; the answer key sits one level up in ``key.json``, so the page
cannot give the answer away. Takes are loudness-matched (same RMS) because a
louder take tends to win a preference test for being louder.
"""

from __future__ import annotations

import argparse
import array
import copy
import datetime as dt
import http.server
import json
import math
import os
import random
import secrets
import shutil
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.build import build_book_script  # noqa: E402
from pipeline.direct import (  # noqa: E402
    _remote_prompt_version,
    apply_pass,
    http_annotate,
    resolve_spec,
    run_pass,
    scene_passages,
)
from pipeline.render import DEFAULT_GATEWAY, Renderer, http_synth  # noqa: E402
from pipeline.schema import BookScript  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:  # a Windows console defaults to cp1252; the passages are full of curly quotes
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASSAGES = ROOT / "docs" / "experiments" / "direction-ab" / "passages.json"
DEFAULT_WORK = ROOT / "output" / "direction-ab"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

CONDITIONS = ("A", "B", "C")
PARTS = {1: ("A", "B"), 2: ("B", "C")}
#: B must win at least this share of part-1 passages (6 of 8 passages).
THRESHOLD = 0.75
#: Every take is matched to this RMS level (dBFS), peaks capped below full scale.
TARGET_RMS_DBFS = -20.0
PEAK_CAP = 0.97


# ---- small helpers -------------------------------------------------------------


def _get_json(url: str, timeout: float = 15.0) -> object:
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(url: str, body: dict, timeout: float = 60.0) -> object:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)


def _passages() -> list[dict]:
    return json.loads(PASSAGES.read_text(encoding="utf-8"))["passages"]


def _config(work: Path) -> dict:
    path = work / "config.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run `prepare` first.")
    return json.loads(path.read_text(encoding="utf-8"))


# ---- prepare: the director pass --------------------------------------------------


def _preflight(gateway: str, engine: str, model: str) -> dict:
    """Real calls, not pings: the engine's live spec, and one director inference."""
    try:
        _get_json(f"{gateway}/api/health")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"gateway {gateway} is not answering ({exc}). Start it first.")
    spec = resolve_spec(engine, gateway)
    if not spec.get("instruction"):
        raise SystemExit(f"engine '{engine}' does not declare instruction: a note would do nothing.")
    annotate = http_annotate(gateway, "ollama", model)
    probe = annotate("The door creaked open, and she held her breath.", [spec])
    picks = {p.get("engine_id"): p for p in probe.get("per_engine") or []}
    note = (picks.get(engine) or {}).get("instruction")
    print(f"preflight  director {model} answered; note for {engine}: {note!r}")
    if not note:
        print("  ! the probe returned no note for this engine; the pass may stamp nothing")
    return spec


def _free_vram(gateway: str, engine: str, model: str | None = None) -> None:
    try:
        _post_json(f"{gateway}/api/unload/{engine}", {})
        print(f"vram       asked the gateway to unload {engine}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"vram       could not unload {engine} ({exc})")
    if model:
        try:  # keep_alive 0 evicts the director model from the GPU now
            _post_json(f"{OLLAMA_URL}/api/generate", {"model": model, "keep_alive": 0}, timeout=120)
            print(f"vram       asked Ollama to unload {model}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"vram       could not unload {model} from Ollama ({exc})")


def _noted(pdir: Path) -> bool:
    """True when this passage already has a directed script with a note on it."""
    path = pdir / "directed.json"
    if not path.exists():
        return False
    script = BookScript.model_validate_json(path.read_text(encoding="utf-8"))
    return any(u.render.instruction for u in script.utterances)


def cmd_prepare(args: argparse.Namespace) -> int:
    work: Path = args.work
    gateway = args.gateway.rstrip("/")
    work.mkdir(parents=True, exist_ok=True)
    _free_vram(gateway, args.engine)  # the director gets the GPU to itself
    spec = _preflight(gateway, args.engine, args.model)
    prompt_version = _remote_prompt_version(gateway)
    _write_json(
        work / "config.json",
        {
            "engine": args.engine,
            "voice": args.voice,
            "model": args.model,
            "gateway": gateway,
            "prompt_version": prompt_version,
            "spec": spec,
            "prepared_at": dt.datetime.now().isoformat(timespec="seconds"),
        },
    )
    annotate = http_annotate(gateway, "ollama", args.model)
    failures = 0
    for p in _passages():
        pdir = work / "passages" / p["id"]
        if _noted(pdir) and not args.redo:
            print(f"skip       {p['id']} (already directed)")
            continue
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "source.txt").write_text(p["text"] + "\n", encoding="utf-8", newline="\n")
        # A scene can come back with no note: the model may judge a passage
        # neutral (the prompt allows null) or the call may fail. The Review
        # screen's answer is Retry, so the harness retries too, and records how
        # many attempts each passage took: that is a finding in itself.
        attempts: list[dict] = []
        for attempt in range(1, args.attempts + 1):
            script = build_book_script(
                str(pdir / "source.txt"),
                engine=args.engine,
                voice_id=args.voice,
                book_type="fiction",
                multi_voice=False,
                title=p["id"],
            )
            pass_ = run_pass(
                script,
                annotate=annotate,
                target_spec=spec,
                model=args.model,
                prompt_version=prompt_version,
                book=p["id"],
                cache={},
            )
            for scene in pass_.scenes:
                # The engine takes no inline tags, so a rejected tagged passage
                # costs nothing: keep the book's text and use the note.
                if scene.status == "kept":
                    scene.decision = "accepted"
                elif scene.status == "rejected" and scene.instruction:
                    scene.decision = "kept_text"
            apply_pass(script, pass_, instruction_engines={args.engine})
            noted = sum(1 for u in script.utterances if u.render.instruction)
            attempts.append(
                {
                    "attempt": attempt,
                    "noted": noted,
                    "scenes": [
                        {"status": s.status, "error": s.error, "emotion": s.emotion, "instruction": s.instruction}
                        for s in pass_.scenes
                    ],
                }
            )
            states = ", ".join(
                f"{s.scene_id}:{s.status}" + (f" ({s.error})" if s.error else "") for s in pass_.scenes
            )
            print(f"directed   {p['id']} try {attempt}: {noted}/{len(script.utterances)} lines noted ({states})")
            if noted:
                break
        _write_json(pdir / "attempts.json", attempts)
        _write_json(pdir / "pass.json", json.loads(pass_.model_dump_json()))
        (pdir / "directed.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
        if noted == 0:
            failures += 1
            print(f"  ! {p['id']} got no note in {len(attempts)} tries; it is left out of the test")
        else:
            for s in pass_.scenes:
                print(f"           note: {s.instruction!r}  emotion: {s.emotion!r}")
    _free_vram(gateway, args.engine, args.model)  # hand the GPU to the TTS engine
    return 1 if failures == len(_passages()) else 0


# ---- render: three takes per passage, loudness-matched, blind ------------------


def _condition_scripts(directed: BookScript, pass_: dict) -> dict[str, BookScript]:
    """A, B and C differ only in ``render.instruction``; speed is pinned to 1.0 so
    a pace the director asked for cannot be mistaken for the note's effect."""
    emotion = {s["scene_id"]: (s.get("emotion") or "").strip() for s in pass_["scenes"]}
    scene_of = {line_id: sp.scene_id for sp in scene_passages(directed) for line_id in sp.line_ids}
    out: dict[str, BookScript] = {}
    for cond in CONDITIONS:
        script = copy.deepcopy(directed)
        for u in script.utterances:
            u.render.speed = 1.0
            if cond == "A":
                u.render.instruction = None
                u.render.direction_source = "none"
            elif cond == "C" and u.render.instruction:
                u.render.instruction = emotion.get(scene_of.get(u.id, ""), "") or "neutral"
        out[cond] = script
    return out


def _read_wav(path: Path) -> tuple[array.array, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise SystemExit(f"{path}: expected 16-bit mono PCM")
        rate = w.getframerate()
        pcm = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm, rate


def _loudness_match(src: Path, dst: Path) -> dict:
    pcm, rate = _read_wav(src)
    n = max(1, len(pcm))
    rms = math.sqrt(sum(s * s for s in pcm) / n) / 32768.0
    peak = max((abs(s) for s in pcm), default=0) / 32768.0
    gain = (10 ** (TARGET_RMS_DBFS / 20)) / rms if rms > 0 else 1.0
    if peak * gain > PEAK_CAP:  # never clip: a capped take ends up a little quieter
        gain = PEAK_CAP / peak
    out = array.array("h", (max(-32768, min(32767, int(round(s * gain)))) for s in pcm))
    if sys.byteorder == "big":
        out.byteswap()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    final_rms = rms * gain
    return {
        "seconds": round(len(pcm) / rate, 2),
        "rms_dbfs_in": round(20 * math.log10(rms), 1) if rms > 0 else None,
        "rms_dbfs_out": round(20 * math.log10(final_rms), 1) if final_rms > 0 else None,
        "peak_capped": final_rms < (10 ** (TARGET_RMS_DBFS / 20)) - 1e-6,
    }


def cmd_render(args: argparse.Namespace) -> int:
    work: Path = args.work
    cfg = _config(work)
    engine, gateway = cfg["engine"], cfg["gateway"]
    renderer = Renderer(
        http_synth(gateway, engine, instruction_engines={engine}),
        str(work / "cache"),
    )
    takes: dict[str, dict[str, Path]] = {}
    for p in _passages():
        pdir = work / "passages" / p["id"]
        directed_path = pdir / "directed.json"
        if not directed_path.exists():
            print(f"skip       {p['id']} (not directed)")
            continue
        directed = BookScript.model_validate_json(directed_path.read_text(encoding="utf-8"))
        if not any(u.render.instruction for u in directed.utterances):
            print(f"skip       {p['id']} (the director gave it no note)")
            continue
        pass_ = json.loads((pdir / "pass.json").read_text(encoding="utf-8"))
        takes[p["id"]] = {}
        for cond, script in _condition_scripts(directed, pass_).items():
            out_dir = pdir / cond
            renderer.render(script, str(out_dir))  # cached: a rerun costs no synth
            wav = out_dir / "chapter_000.wav"
            if not wav.exists():
                raise SystemExit(f"{wav} was not written")
            takes[p["id"]][cond] = wav
            print(f"rendered   {p['id']} {cond}")

    listen = work / "listen"
    if listen.exists():
        shutil.rmtree(listen)
    (listen / "audio").mkdir(parents=True)
    rng = random.SystemRandom()
    key: dict = {"config": cfg, "trials": {}, "takes": {}}
    trials: list[dict] = []
    token_of: dict[tuple[str, str], str] = {}
    for pid, conds in takes.items():
        for cond, wav in conds.items():
            token = secrets.token_hex(6)
            token_of[(pid, cond)] = token
            key["takes"][token] = {"passage": pid, "condition": cond, **_loudness_match(wav, listen / "audio" / f"{token}.wav")}
    by_id = {p["id"]: p for p in _passages()}
    for part, (left, right) in PARTS.items():
        order = list(takes)
        rng.shuffle(order)
        for pid in order:
            pair = [left, right]
            rng.shuffle(pair)
            trial = f"t{secrets.token_hex(4)}"
            directed = BookScript.model_validate_json(
                (work / "passages" / pid / "directed.json").read_text(encoding="utf-8")
            )
            note = next((u.render.instruction for u in directed.utterances if u.render.instruction), "")
            trials.append(
                {
                    "trial": trial,
                    "part": part,
                    "text": by_id[pid]["text"],
                    "note": note,
                    "takes": [f"audio/{token_of[(pid, c)]}.wav" for c in pair],
                }
            )
            key["trials"][trial] = {"passage": pid, "part": part, "takes": pair}
    shutil.copyfile(ROOT / "scripts" / "direction_ab_page.html", listen / "index.html")
    _write_json(listen / "trials.json", {"created": dt.datetime.now().isoformat(timespec="seconds"), "trials": trials})
    _write_json(work / "key.json", key)
    print(f"\nlisten     {len(trials)} trials in {listen} (key kept in {work / 'key.json'})")
    return 0


# ---- serve: the listening page, which saves its answers here -------------------


def cmd_serve(args: argparse.Namespace) -> int:
    listen = args.work / "listen"
    if not (listen / "trials.json").exists():
        raise SystemExit(f"{listen} has no trials: run `render` first.")
    results = listen / "results.json"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(listen), **kw)

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            if self.path != "/api/results":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self.send_error(400, "not JSON")
                return
            body["saved_at"] = dt.datetime.now().isoformat(timespec="seconds")
            _write_json(results, body)
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/results":
                data = results.read_bytes() if results.exists() else b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                return
            super().do_GET()

        def log_message(self, *_a) -> None:  # quiet
            pass

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listen at  http://{args.host}:{args.port}/   (answers save to {results})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ---- score -----------------------------------------------------------------------


def cmd_score(args: argparse.Namespace) -> int:
    work: Path = args.work
    key = json.loads((work / "key.json").read_text(encoding="utf-8"))
    results_path = work / "listen" / "results.json"
    if not results_path.exists():
        raise SystemExit("no results.json yet: nobody has listened.")
    answers = json.loads(results_path.read_text(encoding="utf-8")).get("answers", {})
    lines = []
    summary: dict = {}
    for part, (x, y) in PARTS.items():
        rows = []
        wins = {x: 0, y: 0}
        ratings = {x: [], y: []}
        note_hits = {"right": 0, "wrong": 0, "cant_tell": 0}
        for trial, meta in key["trials"].items():
            if meta["part"] != part or trial not in answers:
                continue
            a = answers[trial]
            if a.get("keep") not in (0, 1):
                continue
            winner = meta["takes"][a["keep"]]
            wins[winner] += 1
            for i, cond in enumerate(meta["takes"]):
                r = (a.get("ratings") or [None, None])[i]
                if isinstance(r, (int, float)):
                    ratings[cond].append(r)
            follows = a.get("follows")
            if part == 1 and follows is not None:
                if follows == "cant_tell":
                    note_hits["cant_tell"] += 1
                elif meta["takes"][follows] == "B":
                    note_hits["right"] += 1
                else:
                    note_hits["wrong"] += 1
            rows.append((meta["passage"], winner, a.get("ratings"), a.get("follows")))
        n = sum(wins.values())
        if n == 0:
            continue
        mean = {c: (round(sum(v) / len(v), 2) if v else None) for c, v in ratings.items()}
        summary[part] = {"n": n, "wins": wins, "mean_rating": mean, "note_audible": note_hits}
        lines.append(f"\nPart {part}: {x} vs {y}  ({n} passages answered)")
        for pid, winner, r, f in sorted(rows):
            lines.append(f"  {pid:<26} kept {winner}   ratings {r}   follows-note {f}")
        lines.append(f"  wins: {wins}   mean rating (1-5): {mean}")
        if part == 1:
            share = wins["B"] / n
            verdict = "PASS" if share >= THRESHOLD else "FAIL"
            lines.append(
                f"  B (directed) kept on {wins['B']}/{n} = {share:.0%}; threshold {THRESHOLD:.0%} -> {verdict}"
            )
            lines.append(f"  the note was audible (picked the directed take as following it): {note_hits}")
            summary[part]["verdict"] = verdict
    text = "\n".join(lines)
    print(text)
    _write_json(work / "score.json", summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK, help="default: output/direction-ab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prepare", help="build each passage and run the director pass")
    pr.add_argument("--engine", required=True)
    pr.add_argument("--voice", required=True)
    pr.add_argument("--model", default="gemma3:12b")
    pr.add_argument("--gateway", default=DEFAULT_GATEWAY)
    pr.add_argument("--attempts", type=int, default=3, help="director tries per passage (default 3)")
    pr.add_argument("--redo", action="store_true", help="direct passages that already have a note again")
    sub.add_parser("render", help="render A/B/C and build the blind listening folder")
    sv = sub.add_parser("serve", help="serve the listening page")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=int(os.environ.get("DIRECTION_AB_PORT", "5197")))
    sub.add_parser("score", help="unblind the answers and apply the threshold")
    args = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "render": cmd_render, "serve": cmd_serve, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
