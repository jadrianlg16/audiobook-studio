"""End-to-end: a book file -> a playable output folder.

    python -m pipeline.generate pipeline/samples/sample.txt \
        --title "The Cold Lantern" --out output/cold-lantern

    # force a genre, or classify with a local LLM instead of the heuristic:
    python -m pipeline.generate book.txt --out output/book --book-type nonfiction
    python -m pipeline.generate book.txt --out output/book --llm-classify --ollama-url http://localhost:11434

Produces in ``--out``: chapter_NNN.wav, manifest.json, player.html, .cache/.
Then:  cd <out> && python -m http.server 8100  -> open /player.html
"""

from __future__ import annotations

import argparse
import os
import shutil

from .build import build_book_script
from .cast import CASTING_MODES
from .lexicon import load_lexicon, make_text_transform
from .render import DEFAULT_GATEWAY, Renderer, fake_synth, http_synth

_PLAYER_SRC = os.path.join(os.path.dirname(__file__), "player", "player.html")


def generate(
    path: str,
    out_dir: str,
    *,
    voice: str | None = None,
    engine: str = "kokoro",
    title: str = "",
    author: str = "",
    book_type: str = "auto",
    multi_voice: bool | None = None,
    use_llm: bool = False,
    llm_kwargs: dict | None = None,
    gateway_url: str = DEFAULT_GATEWAY,
    offline: bool = False,
    normalize: bool = True,
    progress: bool = True,
    casting_mode: str = "narrator_only",
    expressive_engine: str = "breeze",
    lexicon_paths: list[str] | None = None,
) -> str:
    """Run classify -> build -> render and write the output folder. Returns out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    script = build_book_script(
        path,
        voice_id=voice,
        engine=engine,
        title=title,
        author=author,
        book_type=book_type,
        multi_voice=multi_voice,
        use_llm=use_llm,
        llm_kwargs=llm_kwargs,
        casting_mode=casting_mode,
        expressive_engine=expressive_engine,
    )
    if progress:
        print(f"parsed: {script.stats()}", flush=True)
        if len(script.cast) > 1:
            print(
                "cast: "
                + ", ".join(
                    f"{c.character}={c.voice_id or c.voice_design}({c.engine})" for c in script.cast
                ),
                flush=True,
            )

    # A pronunciation lexicon is just a text_transform the renderer applies
    # before the cache key is computed — no lexicon means no transform at all,
    # so an unset --lexicon changes nothing about existing behaviour.
    text_transform = None
    if lexicon_paths:
        lexicon = load_lexicon(*lexicon_paths)
        if lexicon:
            text_transform = make_text_transform(lexicon)

    synth = fake_synth() if offline else http_synth(gateway_url, engine)
    renderer = Renderer(
        synth,
        # Fake clips must never be mistaken for real ones later (same key,
        # different answer), so the offline run keeps a cache of its own.
        cache_dir=os.path.join(out_dir, ".cache-offline" if offline else ".cache"),
        normalize=normalize,
        progress=progress,
        text_transform=text_transform,
    )
    renderer.render(script, out_dir)

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        fh.write(script.model_dump_json(indent=2))
    if os.path.exists(_PLAYER_SRC):
        shutil.copyfile(_PLAYER_SRC, os.path.join(out_dir, "player.html"))

    if progress:
        secs = sum(c.duration_ms for c in script.chapters) / 1000
        prof = script.profile.label if script.profile else "?"
        print(f"done: {prof} · {len(script.chapters)} chapters · {secs:.1f}s -> {out_dir}", flush=True)
    return out_dir


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Generate an audiobook from a text file.")
    ap.add_argument("path", help="path to a .txt / markdown book file")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--voice", default=None, help="override the profile's narrator voice")
    ap.add_argument("--engine", default="kokoro")
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--book-type", default="auto", help="auto | literary | nonfiction | poetry | selfhelp | ...")
    ap.add_argument("--single-voice", action="store_true", help="force one narrator (no attribution)")
    ap.add_argument("--multi-voice", action="store_true", help="force multi-voice even for non-fiction")
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY, help="gateway base URL")
    ap.add_argument("--offline", action="store_true", help="use the fake synth (no gateway)")
    ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument(
        "--casting-mode", default="narrator_only", choices=list(CASTING_MODES),
        help="hybrid casting: who moves onto --expressive-engine",
    )
    ap.add_argument("--expressive-engine", default="breeze", help="engine for characters moved off the narrator engine")
    ap.add_argument("--lexicon", default=None, help="path to a pronunciation lexicon JSON file")
    # LLM classification (optional; falls back to the heuristic on any failure).
    ap.add_argument("--llm-classify", action="store_true", help="classify book type with an LLM")
    ap.add_argument("--classify-provider", default="ollama", choices=["ollama", "gemini"])
    ap.add_argument("--classify-model", default="llama3.2")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--gemini-key", default="")
    args = ap.parse_args(argv)

    llm_kwargs = {
        "provider": args.classify_provider,
        "model": args.classify_model,
        "ollama_url": args.ollama_url,
        "gemini_key": args.gemini_key,
    }
    mv = True if args.multi_voice else False if args.single_voice else None
    generate(
        args.path,
        args.out,
        voice=args.voice,
        engine=args.engine,
        title=args.title,
        author=args.author,
        book_type=args.book_type,
        multi_voice=mv,
        use_llm=args.llm_classify,
        llm_kwargs=llm_kwargs,
        gateway_url=args.gateway,
        offline=args.offline,
        normalize=not args.no_normalize,
        casting_mode=args.casting_mode,
        expressive_engine=args.expressive_engine,
        lexicon_paths=[args.lexicon] if args.lexicon else None,
    )


if __name__ == "__main__":
    main()
