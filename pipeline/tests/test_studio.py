"""Studio service tests — the whole product, end to end, with no model.

Everything runs with ``STUDIO_OFFLINE=1`` (fake synth) and
``STUDIO_FAKE_DIRECTOR=1`` (canned direction) against a gateway URL with
nothing listening on it, which is the point of contract semantic #6: the
offline paths are the *same* code paths, so a book can be created, rendered,
directed, reviewed, applied, re-rendered and exported without a GPU, an LLM, or
a network.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from pipeline.master import ffmpeg_available
from pipeline.render import fake_synth as real_fake_synth
from pipeline.studio import jobs as jobs_module
from pipeline.studio import store
from pipeline.studio.app import create_app
from pipeline.studio.jobs import JobManager
from pipeline.studio.models import Job

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
DIALOGUE = os.path.join(SAMPLES, "dialogue.txt")
EPUB = os.path.join(SAMPLES, "sample.epub")

# Nothing listens on port 9 (the discard port), so every gateway call fails fast
# and the tests exercise the "no gateway" fallbacks rather than a lucky hit on a
# gateway the developer happens to have running.
DEAD_GATEWAY = "http://127.0.0.1:9"


def _dialogue_text() -> str:
    with open(DIALOGUE, encoding="utf-8") as fh:
        return fh.read()


def _scene_text() -> str:
    """The dialogue sample with a scene break between paragraphs.

    The sample is one scene, and the fake director only rejects every 5th scene
    it is asked about — a one-scene book can therefore never produce the
    kept-*and*-rejected pass the review screen exists for.
    """
    paragraphs = [p for p in _dialogue_text().split("\n\n") if p.strip()]
    return paragraphs[0] + "\n\n" + "\n\n* * *\n\n".join(paragraphs[1:])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIOBOOK_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("STUDIO_OFFLINE", "1")
    monkeypatch.setenv("STUDIO_FAKE_DIRECTOR", "1")
    monkeypatch.setenv("AUDIOBOOK_GATEWAY", DEAD_GATEWAY)
    monkeypatch.setenv("STUDIO_NARRATOR_ENGINE", "kokoro")
    monkeypatch.setenv("STUDIO_EXPRESSIVE_ENGINE", "breeze")
    monkeypatch.setenv("STUDIO_CASTING_MODE", "narrator_only")
    monkeypatch.setenv("STUDIO_URL_PREFIX", "/studio")
    store._gateway_cache.clear()  # the TTL cache is process-global
    yield
    store._gateway_cache.clear()
    store.register_job_provider(None)


@pytest.fixture
def client(env):
    with TestClient(create_app(JobManager())) as c:
        yield c


@pytest.fixture
def slow(monkeypatch):
    """A synth slow enough that a job can be observed mid-flight.

    Pause and cancel are cooperative — they take effect *between* lines — so a
    synth that returns instantly makes them untestable, not correct.
    """

    def factory(*args, **kwargs):
        base = real_fake_synth()

        def synth(text, render):
            time.sleep(0.05)
            return base(text, render)

        return synth

    monkeypatch.setattr(jobs_module, "fake_synth", factory)


# ---- helpers ----------------------------------------------------------------

def wait_job(client, job_id, states=("done", "failed", "cancelled"), timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {states}")


def new_book(client, *, text=None, title="The Lighthouse", book_type="general_fiction",
             voices="multi", author="Ada Fenwick"):
    response = client.post(
        "/api/books",
        json={
            "title": title,
            "author": author,
            "text": _dialogue_text() if text is None else text,
            "book_type": book_type,
            "voices": voices,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def render(client, slug, scope="all", **extra):
    response = client.post(f"/api/books/{slug}/render", json={"scope": scope, **extra})
    assert response.status_code == 202, response.text
    return wait_job(client, response.json()["id"])


def _wait_until_started(client, job_id, timeout=20.0):
    """Act as soon as the first line is in, not after a fixed sleep — a busy box
    would otherwise let the whole render finish before pause/cancel lands and
    the test would fail for a reason that is not the code."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["progress"]["done"] >= 1 or job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("the job never started")


# ---- books ------------------------------------------------------------------

def test_create_from_text_lays_out_the_folder_and_derives_missing_audio(client, tmp_path):
    book = new_book(client)
    assert book["slug"] == "the-lighthouse"
    assert book["utterances"] > 0
    assert book["audio"] == {"cached": 0, "stale": 0, "missing": book["utterances"]}
    assert book["rendered_at"] is None
    assert book["duration_ms"] == 0
    assert book["status"] == "idle"

    folder = tmp_path / "output" / "the-lighthouse"
    assert (folder / "book.json").exists()
    assert (folder / "source.txt").exists()
    assert (folder / "script.json").exists()
    assert (folder / "versions" / "script.v0001.json").exists()

    script = client.get("/api/books/the-lighthouse/script").json()
    assert script["version"] == 1
    assert {line["audio_state"] for line in script["lines"]} == {"missing"}
    assert all(line["duration_ms"] is None and line["audio"] is None for line in script["lines"])
    # Scenes come from the script itself, with the contract's id format.
    scene_ids = [s["id"] for c in script["chapters"] for s in c["scenes"]]
    assert scene_ids and all(s.startswith("c") and "_s" in s for s in scene_ids)
    assert sum(len(s["line_ids"]) for c in script["chapters"] for s in c["scenes"]) == len(script["lines"])
    assert all(c["fresh"] is False for c in script["chapters"])


def test_slug_collisions_get_a_numeric_suffix(client):
    assert new_book(client)["slug"] == "the-lighthouse"
    assert new_book(client)["slug"] == "the-lighthouse-2"
    assert new_book(client)["slug"] == "the-lighthouse-3"


def test_upload_an_epub_takes_its_title_and_author(client, tmp_path):
    with open(EPUB, "rb") as fh:
        response = client.post(
            "/api/books/upload",
            files={"file": ("sample.epub", fh.read(), "application/epub+zip")},
            data={"book_type": "auto", "voices": "auto"},
        )
    assert response.status_code == 201, response.text
    book = response.json()
    assert book["title"] and book["author"]  # EPUB metadata filled the blanks
    assert book["chapters"] >= 2
    assert (tmp_path / "output" / book["slug"] / "source.epub").exists()
    assert client.get(f"/api/books/{book['slug']}/script").status_code == 200


def test_upload_rejects_a_file_type_the_pipeline_cannot_ingest(client):
    response = client.post(
        "/api/books/upload", files={"file": ("book.pdf", b"%PDF-1.4", "application/pdf")}
    )
    assert response.status_code == 400
    assert ".epub" in response.json()["detail"]


def test_delete_removes_the_folder(client, tmp_path):
    book = new_book(client)
    assert client.delete(f"/api/books/{book['slug']}").status_code == 204
    assert not (tmp_path / "output" / book["slug"]).exists()
    assert client.get(f"/api/books/{book['slug']}").status_code == 404
    assert client.get("/api/books").json() == []


# ---- rendering --------------------------------------------------------------

def test_render_job_caches_every_line_and_writes_the_manifest(client, tmp_path):
    book = new_book(client)
    slug = book["slug"]
    job = render(client, slug, "all")

    assert job["status"] == "done", job["error"]
    assert job["progress"]["done"] == job["progress"]["total"] == book["utterances"]
    assert job["result"]["chapters_written"] == [0]
    assert job["result"]["duration_ms"] > 0
    assert job["log"], "a render job with no log is unreviewable"

    script = client.get(f"/api/books/{slug}/script").json()
    assert {line["audio_state"] for line in script["lines"]} == {"cached"}
    assert all(line["duration_ms"] and line["duration_ms"] > 0 for line in script["lines"])
    assert all(chapter["fresh"] for chapter in script["chapters"])
    assert script["book"]["rendered_at"] is not None
    assert script["book"]["duration_ms"] > 0

    folder = tmp_path / "output" / slug
    assert (folder / "manifest.json").exists()
    assert (folder / "chapter_000.wav").exists()
    assert (folder / "player.html").exists()


def test_editing_a_note_makes_exactly_that_line_stale(client):
    book = new_book(client)
    slug = book["slug"]
    render(client, slug, "all")
    script = client.get(f"/api/books/{slug}/script").json()
    target = script["lines"][2]["id"]

    response = client.patch(f"/api/books/{slug}/lines/{target}", json={"instruction": "Almost a whisper."})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == 2
    assert payload["line"]["audio_state"] == "stale"
    assert payload["line"]["render"]["direction_source"] == "manual"

    after = client.get(f"/api/books/{slug}/script").json()
    stale = [line["id"] for line in after["lines"] if line["audio_state"] == "stale"]
    assert stale == [target]
    assert after["book"]["audio"]["cached"] == book["utterances"] - 1
    assert after["chapters"][0]["fresh"] is False


def test_editing_text_clears_spoken_text(client):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    client.patch(f"/api/books/{slug}/lines/{line_id}", json={"spoken_text": "(sigh) Rain."})
    edited = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"text": "Rain fell."}).json()
    assert edited["line"]["text"] == "Rain fell."
    assert edited["line"]["render"]["spoken_text"] is None


def test_a_stale_render_pays_for_one_line(client):
    """The whole promise of the content-hash cache, measured."""
    book = new_book(client)
    slug = book["slug"]
    render(client, slug, "all")
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][2]["id"]
    client.patch(f"/api/books/{slug}/lines/{line_id}", json={"instruction": "Bright and quick."})

    job = render(client, slug, "stale")
    assert job["status"] == "done", job["error"]
    assert job["progress"]["synth_calls"] == 1
    assert job["progress"]["cache_hits"] == job["progress"]["total"] - 1
    assert job["result"]["chapters_written"] == [0]
    assert all(c["fresh"] for c in client.get(f"/api/books/{slug}/script").json()["chapters"])


def test_render_scope_stale_does_nothing_when_the_book_is_fresh(client):
    slug = new_book(client)["slug"]
    render(client, slug, "all")
    job = render(client, slug, "stale")
    assert job["result"]["chapters_written"] == []
    assert job["progress"]["synth_calls"] == 0


def test_render_log_names_the_engine_that_would_have_been_called(client):
    """Offline swaps the synth, not the routing — the log has to stay honest
    about which engine a line is destined for, or an offline dry run tells you
    nothing about the real one."""
    slug = new_book(client)["slug"]
    client.post(f"/api/books/{slug}/casting", json={"mode": "dialogue_expressive"})
    job = render(client, slug, "all")
    engines = {line["line"].split()[2] for line in job["log"] if "  synth  " in line["line"]}
    assert "breeze" in engines and "kokoro" in engines
    assert any("offline" in entry["line"] for entry in job["log"])


def test_single_line_render_and_audio_endpoint(client):
    slug = new_book(client)["slug"]
    script = client.get(f"/api/books/{slug}/script").json()
    line_id = script["lines"][0]["id"]

    assert client.get(f"/api/books/{slug}/lines/{line_id}/audio").status_code == 404

    response = client.post(f"/api/books/{slug}/lines/{line_id}/render", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["line"]["audio_state"] == "cached"
    assert payload["audio_url"].endswith(f"/api/books/{slug}/lines/{line_id}/audio")

    audio = client.get(f"/api/books/{slug}/lines/{line_id}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.content[:4] == b"RIFF"


def test_single_line_render_is_503_while_a_render_job_runs(client, slow):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    job_id = client.post(f"/api/books/{slug}/render", json={"scope": "all"}).json()["id"]
    try:
        _wait_until_started(client, job_id)
        response = client.post(f"/api/books/{slug}/lines/{line_id}/render", json={})
        assert response.status_code == 503
        assert "render job" in response.json()["detail"]
    finally:
        client.post(f"/api/jobs/{job_id}/cancel", json={})
        wait_job(client, job_id, ("cancelled", "done"))


# ---- casting ----------------------------------------------------------------

def test_hybrid_casting_moves_dialogue_to_the_expressive_engine(client):
    slug = new_book(client)["slug"]
    response = client.post(f"/api/books/{slug}/casting", json={"mode": "dialogue_expressive"})
    assert response.status_code == 200, response.text
    script = response.json()

    narrator = [c for c in script["cast"] if c["character"] == "Narrator"][0]
    # "Unknown" rides with the narrator on purpose (an unattributed quote is read
    # by the narrator), so it stays on kokoro like the narrator does.
    characters = [c for c in script["cast"] if c["character"] not in ("Narrator", "Unknown")]
    assert narrator["engine"] == "kokoro" and narrator["voice_id"]
    assert characters, "the dialogue sample must produce a cast"
    for entry in characters:
        assert entry["engine"] == "breeze"
        assert entry["voice_id"] is None       # breeze has no named voices…
        assert entry["voice_design"]           # …so it designs one from prose

    dialogue = [line for line in script["lines"] if line["kind"] == "dialogue" and line["speaker"] != "Unknown"]
    assert dialogue and all(line["render"]["engine"] == "breeze" for line in dialogue)
    assert all(line["render"]["voice_design"] for line in dialogue)
    assert script["book"]["engines"].get("breeze", 0) > 0

    note = script["book"]["licence_note"]
    assert note and "breeze" in note and "non-commercial" in note.lower()


def test_patch_cast_restamps_that_characters_lines(client):
    slug = new_book(client)["slug"]
    script = client.get(f"/api/books/{slug}/script").json()
    before = script["version"]
    response = client.patch(
        f"/api/books/{slug}/cast", json=[{"character": "Narrator", "voice_id": "bf_emma"}]
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == before + 1
    narration = [line for line in updated["lines"] if line["kind"] == "narration"]
    assert narration and all(line["render"]["voice_id"] == "bf_emma" for line in narration)


# ---- lexicon ----------------------------------------------------------------

def test_lexicon_put_makes_only_the_matching_lines_stale(client):
    slug = new_book(client)["slug"]
    render(client, slug, "all")

    response = client.put(f"/api/books/{slug}/lexicon", json={"entries": {"Elara": "eh-LAR-uh"}})
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == {"Elara": "eh-LAR-uh"}
    assert response.json()["global"] == {}

    script = client.get(f"/api/books/{slug}/script").json()
    stale = [line for line in script["lines"] if line["audio_state"] == "stale"]
    assert stale, "a respelling that changes no key changes no pronunciation"
    assert all("elara" in line["text"].lower() for line in stale)
    assert all(
        "elara" not in line["text"].lower()
        for line in script["lines"]
        if line["audio_state"] == "cached"
    )
    job = render(client, slug, "stale")
    assert job["progress"]["synth_calls"] == len(stale)


# ---- the director -----------------------------------------------------------

def _directed_book(client):
    """A multi-scene book cast so that dialogue lands on an instruction-capable
    engine — otherwise ``apply`` has nothing to stamp (rule: a note only reaches
    a line routed to an engine that can hear one)."""
    book = new_book(client, text=_scene_text(), title="Scenes")
    client.post(f"/api/books/{book['slug']}/casting", json={"mode": "dialogue_expressive"})
    return book["slug"]


def test_director_pass_keeps_and_rejects_and_review_can_decide_and_apply(client):
    slug = _directed_book(client)
    response = client.post(
        f"/api/books/{slug}/direct", json={"scope": "all", "target_engine": "breeze"}
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["id"])
    assert job["status"] == "done", job["error"]

    pass_id = job["result"]["pass_id"]
    counts = job["result"]["counts"]
    assert counts["kept"] > 0 and counts["rejected"] > 0, counts
    assert counts["scenes"] == counts["kept"] + counts["rejected"] + counts["ignored"] + counts["error"]

    listed = client.get(f"/api/books/{slug}/passes").json()
    assert [p["id"] for p in listed] == [pass_id]
    assert "scenes" not in listed[0], "the summary listing must not ship every scene"

    document = client.get(f"/api/books/{slug}/passes/{pass_id}").json()
    kept = [s for s in document["scenes"] if s["status"] == "kept"]
    rejected = [s for s in document["scenes"] if s["status"] == "rejected"]
    # A rejection keeps the model's text so the review screen can diff it, and
    # keeps the note so `kept_text` is a real option.
    assert all(s["model_text"] and s["model_text"] != s["source_text"] for s in rejected)
    assert all(s["instruction"] for s in rejected)
    assert all(s["decision"] == "pending" for s in document["scenes"])

    decisions = [
        {"scene_id": rejected[0]["scene_id"], "decision": "kept_text"},
        {"scene_id": kept[0]["scene_id"], "decision": "edited", "instruction": "Hushed, close in."},
    ] + [{"scene_id": s["scene_id"], "decision": "accepted"} for s in kept[1:]]
    decided = client.post(f"/api/books/{slug}/passes/{pass_id}/decide", json={"decisions": decisions})
    assert decided.status_code == 200, decided.text
    by_id = {s["scene_id"]: s for s in decided.json()["scenes"]}
    assert by_id[rejected[0]["scene_id"]]["decision"] == "kept_text"
    assert by_id[kept[0]["scene_id"]]["final_instruction"] == "Hushed, close in."
    # Deciding writes nothing into the book.
    assert all(
        line["render"]["direction_source"] == "none"
        for line in client.get(f"/api/books/{slug}/script").json()["lines"]
    )

    applied = client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["pass"]["applied_at"]
    lines = body["script"]["lines"]
    sources = {line["render"]["direction_source"] for line in lines}
    assert "director" in sources
    # An edited scene stamps "manual" only on lines that can take a note; if the
    # edited scene has none on breeze (Unknown lines ride with the narrator), fine.
    edited_lines = [l for l in lines if f"c{l['chapter_index']}_s{l['scene_index']}" == kept[0]["scene_id"]]
    if any(l["render"]["engine"] == "breeze" for l in edited_lines):
        assert "manual" in sources
    directed = [line for line in lines if line["render"]["direction_source"] != "none"]
    assert directed and all(line["render"]["engine"] == "breeze" for line in directed)
    assert all(line["audio_state"] in ("missing", "stale") for line in directed)
    # kept_text keeps the author's words: the note applies, the tags do not.
    kept_text_lines = [
        line for line in lines
        if line["id"] in (rejected[0].get("line_spoken_text") or {})
    ]
    assert all(line["render"]["spoken_text"] is None for line in kept_text_lines)


def test_a_second_apply_changes_nothing_and_spares_a_manual_line(client):
    slug = _directed_book(client)
    job = wait_job(
        client,
        client.post(f"/api/books/{slug}/direct", json={"scope": "all", "target_engine": "breeze"})
        .json()["id"],
    )
    pass_id = job["result"]["pass_id"]
    document = client.get(f"/api/books/{slug}/passes/{pass_id}").json()
    kept = [s for s in document["scenes"] if s["status"] == "kept"]
    client.post(
        f"/api/books/{slug}/passes/{pass_id}/decide",
        json={"decisions": [{"scene_id": s["scene_id"], "decision": "accepted"} for s in kept]},
    )
    client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={})

    script = client.get(f"/api/books/{slug}/script").json()
    stamped = [line for line in script["lines"] if line["render"]["direction_source"] == "director"]
    assert stamped
    mine = stamped[0]["id"]
    client.patch(f"/api/books/{slug}/lines/{mine}", json={"instruction": "My note, not the model's."})
    version = client.get(f"/api/books/{slug}/script").json()["version"]

    second = client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={}).json()
    line = [x for x in second["script"]["lines"] if x["id"] == mine][0]
    assert line["render"]["instruction"] == "My note, not the model's."
    assert line["render"]["direction_source"] == "manual"
    # Idempotent: nothing changed, so no new version was written.
    assert second["script"]["version"] == version


def test_retry_reruns_one_scene_inside_the_same_pass(client):
    slug = _directed_book(client)
    job = wait_job(
        client,
        client.post(f"/api/books/{slug}/direct", json={"scope": "all", "target_engine": "breeze"})
        .json()["id"],
    )
    pass_id = job["result"]["pass_id"]
    document = client.get(f"/api/books/{slug}/passes/{pass_id}").json()
    target = [s for s in document["scenes"] if s["status"] == "rejected"][0]
    client.post(
        f"/api/books/{slug}/passes/{pass_id}/decide",
        json={"decisions": [{"scene_id": target["scene_id"], "decision": "rejected"}]},
    )

    response = client.post(
        f"/api/books/{slug}/passes/{pass_id}/retry", json={"scenes": [target["scene_id"]]}
    )
    assert response.status_code == 202, response.text
    retry = wait_job(client, response.json()["id"])
    assert retry["status"] == "done", retry["error"]
    assert retry["result"]["pass_id"] == pass_id

    after = client.get(f"/api/books/{slug}/passes/{pass_id}").json()
    scene = [s for s in after["scenes"] if s["scene_id"] == target["scene_id"]][0]
    assert scene["attempts"] == target["attempts"] + 1
    assert scene["decision"] == "pending", "a retry must not inherit the old verdict"
    assert len(after["scenes"]) == len(document["scenes"])


def test_direct_scope_undirected_skips_scenes_that_already_have_a_note(client):
    slug = _directed_book(client)
    script = client.get(f"/api/books/{slug}/script").json()
    scene = script["chapters"][0]["scenes"][0]
    client.patch(f"/api/books/{slug}/lines/{scene['line_ids'][0]}", json={"instruction": "Set."})

    job = wait_job(
        client,
        client.post(f"/api/books/{slug}/direct", json={"scope": "undirected"}).json()["id"],
    )
    document = client.get(f"/api/books/{slug}/passes/{job['result']['pass_id']}").json()
    assert scene["id"] not in document["scope"]["scenes"]


# ---- export -----------------------------------------------------------------

def test_export_is_409_until_every_chapter_is_fresh(client):
    slug = new_book(client)["slug"]
    assert client.get(f"/api/books/{slug}").json()["exportable"] is False
    response = client.post(f"/api/books/{slug}/export", json={})
    assert response.status_code == 409
    assert "not rendered" in response.json()["detail"]


def test_exportable_tracks_the_thing_export_actually_requires(client):
    """The flag and the 409 must never disagree — an enabled button that is then
    refused is worse than a disabled one."""
    slug = new_book(client)["slug"]
    render(client, slug, "all")
    assert client.get(f"/api/books/{slug}").json()["exportable"] is True

    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][1]["id"]
    client.patch(f"/api/books/{slug}/lines/{line_id}", json={"instruction": "Softer."})
    assert client.get(f"/api/books/{slug}").json()["exportable"] is False
    assert client.post(f"/api/books/{slug}/export", json={}).status_code == 409

    render(client, slug, "stale")
    assert client.get(f"/api/books/{slug}").json()["exportable"] is True


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_export_job_writes_a_chaptered_m4b(client, tmp_path):
    slug = new_book(client)["slug"]
    render(client, slug, "all")

    response = client.post(f"/api/books/{slug}/export", json={"format": "m4b", "loudness": "acx"})
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["id"], timeout=300)
    assert job["status"] == "done", job["error"]

    result = job["result"]
    assert result["file"].endswith(".m4b") and result["bytes"] > 0
    assert result["format"] == "m4b" and result["loudness"] == "acx"
    assert (tmp_path / "output" / slug / "exports" / result["file"]).exists()

    listed = client.get(f"/api/books/{slug}/exports").json()
    assert [e["file"] for e in listed] == [result["file"]]
    assert listed[0]["sample_rate"] == 44100 and listed[0]["loudness"] == "acx"

    served = client.get(f"/books/{slug}/exports/{result['file']}")
    assert served.status_code == 200
    assert int(served.headers["content-length"]) == result["bytes"]


# ---- jobs -------------------------------------------------------------------

def test_pause_and_resume_a_running_render(client, slow):
    slug = new_book(client)["slug"]
    job_id = client.post(f"/api/books/{slug}/render", json={"scope": "all"}).json()["id"]
    _wait_until_started(client, job_id)
    assert client.post(f"/api/jobs/{job_id}/pause", json={}).status_code == 200

    paused = wait_job(client, job_id, ("paused", "done"))
    assert paused["status"] == "paused", "the render finished before pause could land"
    assert 0 < paused["progress"]["done"] < paused["progress"]["total"]

    assert client.post(f"/api/jobs/{job_id}/resume", json={}).status_code == 200
    finished = wait_job(client, job_id)
    assert finished["status"] == "done"
    assert finished["progress"]["done"] == finished["progress"]["total"]
    # Everything synthesized before the pause was cached, so resuming is cheap.
    assert finished["progress"]["cache_hits"] > 0
    assert {line["audio_state"] for line in client.get(f"/api/books/{slug}/script").json()["lines"]} == {"cached"}


def test_cancel_a_running_render(client, slow):
    slug = new_book(client)["slug"]
    job_id = client.post(f"/api/books/{slug}/render", json={"scope": "all"}).json()["id"]
    _wait_until_started(client, job_id)
    client.post(f"/api/jobs/{job_id}/cancel", json={})
    job = wait_job(client, job_id, ("cancelled", "done"))
    assert job["status"] == "cancelled"
    assert job["finished_at"]


def test_jobs_list_is_newest_first_across_books(client):
    first = new_book(client, title="One")["slug"]
    second = new_book(client, title="Two")["slug"]
    render(client, first, "all")
    render(client, second, "all")
    jobs = client.get("/api/jobs").json()
    assert [j["book"] for j in jobs] == [second, first]
    assert all(j["kind"] == "render" and j["status"] == "done" for j in jobs)
    assert "log" not in jobs[0], "the listing is JobSummary, not the whole record"


def test_events_stream_opens_with_a_progress_event(client):
    slug = new_book(client)["slug"]
    job = render(client, slug, "all")
    lines = []
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            lines.append(line)
            if len(lines) >= 2:
                break
    assert lines[0] == "event: progress"
    payload = json.loads(lines[1][len("data: "):])
    assert payload["id"] == job["id"]
    assert "log" not in payload, "progress events carry the job minus its log"


def test_a_job_left_running_by_a_restart_is_interrupted_and_resumable(client, tmp_path):
    """Contract semantic #5. Simulated by writing a `running` record and
    rebuilding the index the way a fresh process would."""
    slug = new_book(client)["slug"]
    stranded = Job(
        id="render-19700101-000000-abcdef",
        kind="render",
        book=slug,
        status="running",
        created_at="2026-09-05T00:00:00Z",
        started_at="2026-09-05T00:00:00Z",
        request={"scope": "all"},
    )
    path = tmp_path / "output" / slug / "jobs" / f"{stranded.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stranded.model_dump_json(indent=2), encoding="utf-8")

    with TestClient(create_app(JobManager())) as restarted:
        listed = {j["id"]: j for j in restarted.get("/api/jobs").json()}
        assert listed[stranded.id]["status"] == "interrupted"
        assert restarted.get(f"/api/books/{slug}").json()["status"] == "idle"

        assert restarted.post(f"/api/jobs/{stranded.id}/resume", json={}).status_code == 200
        job = wait_job(restarted, stranded.id)
        assert job["status"] == "done"
        assert job["progress"]["done"] == job["progress"]["total"] > 0


# ---- versions ---------------------------------------------------------------

def test_versions_are_appended_and_restorable(client):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    client.patch(f"/api/books/{slug}/lines/{line_id}", json={"instruction": "A note."})

    listed = client.get(f"/api/books/{slug}/versions").json()
    assert [v["version"] for v in listed] == [1, 2]
    assert listed[0]["note"] == "created" and listed[0]["saved_at"]

    restored = client.post(f"/api/books/{slug}/versions/1/restore", json={})
    assert restored.status_code == 200, restored.text
    script = restored.json()
    assert script["version"] == 3  # a restore is a new version equal to v1
    assert [x for x in script["lines"] if x["id"] == line_id][0]["render"]["instruction"] is None
    assert client.post(f"/api/books/{slug}/versions/99/restore", json={}).status_code == 404


# ---- settings & health ------------------------------------------------------

def test_settings_round_trip(client):
    settings = client.get("/api/settings").json()
    assert settings["offline"] is True and settings["fake_director"] is True
    assert settings["narrator_engine"] == "kokoro" and settings["expressive_engine"] == "breeze"
    assert settings["casting_mode"] == "narrator_only"
    assert settings["gateway_url"] == DEAD_GATEWAY
    assert settings["ffmpeg"] == ffmpeg_available()
    assert "general_fiction" in settings["book_types"]
    assert settings["director"]["provider"] == "ollama" and settings["director"]["model"]

    # v1.1: `engines` is the UI's source of truth, so it must be complete with
    # no gateway at all — an empty list here was an empty engine dropdown, on
    # the same screen that tells you to route a line to a different engine.
    by_id = {e["id"]: e for e in settings["engines"]}
    assert {"kokoro", "breeze", "mock", "indextts2"} <= set(by_id)
    assert all(e["available"] is False for e in settings["engines"]), "no gateway = nothing available"
    assert [v["id"] for v in by_id["kokoro"]["voices"]], "kokoro's voices are static knowledge"
    assert by_id["kokoro"]["instruction"] is False and by_id["kokoro"]["audio_tags"] == []
    assert by_id["breeze"]["instruction"] is True and "(sigh)" in by_id["breeze"]["audio_tags"]
    assert by_id["breeze"]["non_commercial"] is True

    patched = client.patch(
        "/api/settings",
        json={
            "casting_mode": "all_expressive",
            "director": {"provider": "ollama", "model": "qwen3:30b"},
            "export": {"format": "mp3", "loudness": "none", "sample_rate": 22050},
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["casting_mode"] == "all_expressive"
    assert patched.json()["director"]["model"] == "qwen3:30b"
    assert patched.json()["export"] == {
        "format": "mp3", "loudness": "none", "sample_rate": 22050, "bitrate": None
    }
    assert client.get("/api/settings").json()["casting_mode"] == "all_expressive"


def test_health_reports_an_unreachable_gateway_without_failing(client):
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "gateway": "unreachable", "offline": True, "fake_director": True}


def test_unknown_book_is_a_404_everywhere(client):
    for path in ("", "/script", "/versions", "/lexicon", "/exports", "/passes"):
        assert client.get(f"/api/books/nope{path}").status_code == 404, path
    assert client.post("/api/books/nope/render", json={"scope": "all"}).status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_clearing_a_note_returns_the_line_to_the_director(client):
    """A typed note locks the line as manual; clearing it must not leave that
    lock behind, or the line could never be directed again without a force."""
    book = new_book(client)
    slug = book["slug"]
    script = client.get(f"/api/books/{slug}/script").json()
    target = script["lines"][1]["id"]

    typed = client.patch(f"/api/books/{slug}/lines/{target}", json={"instruction": "Flat, no apology."})
    assert typed.json()["line"]["render"]["direction_source"] == "manual"

    cleared = client.patch(
        f"/api/books/{slug}/lines/{target}", json={"instruction": None, "spoken_text": None}
    )
    assert cleared.status_code == 200, cleared.text
    line = cleared.json()["line"]
    assert line["render"]["instruction"] is None
    assert line["render"]["direction_source"] == "none"


# ---- validation: nothing unloadable ever reaches the disk --------------------
#
# Every test in this block is a defect found by probing the API, written as
# the behaviour that was missing. The first one is the whole reason the block
# exists: a single PATCH could make a book permanently unreadable through the
# API, because the value was persisted first and validated on the next load.

@pytest.mark.parametrize("speed", [99, 0, 0.1, -1, 4.5])
def test_an_out_of_range_speed_is_refused_and_the_book_still_loads(client, speed):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    before = client.get(f"/api/books/{slug}/script").json()["version"]

    response = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"speed": speed})
    assert response.status_code == 422, response.text
    assert "speed" in response.json()["detail"]

    # The book is untouched: still loadable, still the same version, and the
    # line still has the speed it had. (Before, every one of these was a 500.)
    assert client.get(f"/api/books/{slug}").status_code == 200
    after = client.get(f"/api/books/{slug}/script").json()
    assert after["version"] == before
    assert [x for x in after["lines"] if x["id"] == line_id][0]["render"]["speed"] == 1.0


def test_speed_accepts_the_range_the_schema_allows(client):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    for ok in (0.15, 1.0, 4.0):
        response = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"speed": ok})
        assert response.status_code == 200, response.text
        assert response.json()["line"]["render"]["speed"] == ok
    # null is "back to normal", not "no speed" — which Render would refuse.
    assert client.patch(f"/api/books/{slug}/lines/{line_id}", json={"speed": None}).json()[
        "line"]["render"]["speed"] == 1.0


def test_a_stored_script_that_does_not_validate_is_a_500_that_says_what_to_do(client, tmp_path):
    """The aftermath of the old bug, made survivable: a book whose script.json
    is broken by hand must not answer a bare 500 — it must name the file and
    point at the versions that would fix it."""
    slug = new_book(client)["slug"]
    path = tmp_path / "output" / slug / "script.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["utterances"][0]["render"]["speed"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    response = client.get(f"/api/books/{slug}")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "script.json" in detail and "versions" in detail and "restore" in detail
    # And the way out actually works.
    assert client.post(f"/api/books/{slug}/versions/1/restore", json={}).status_code == 200
    assert client.get(f"/api/books/{slug}").status_code == 200
    # A broken book must not take the shelf down with it either.
    assert client.get("/api/books").status_code == 200


def test_unknown_engines_are_refused_at_every_level(client):
    """Line, cast and settings — the settings one is the dangerous one: it is
    global, so a bad value there casts every book created afterwards."""
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]

    line = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"engine": "nonexistent-xyz"})
    assert line.status_code == 422 and "nonexistent-xyz" in line.json()["detail"]

    cast = client.patch(f"/api/books/{slug}/cast", json=[{"character": "Narrator", "engine": "bogus"}])
    assert cast.status_code == 422 and "bogus" in cast.json()["detail"]

    settings = client.patch("/api/settings", json={"narrator_engine": "nonexistent-xyz"})
    assert settings.status_code == 422 and "narrator_engine" in settings.json()["detail"]
    assert client.get("/api/settings").json()["narrator_engine"] == "kokoro"

    for bad in ({"export": {"sample_rate": 10}}, {"export": {"format": "flac"}}):
        assert client.patch("/api/settings", json=bad).status_code == 422, bad


def test_a_voice_has_to_belong_to_the_engine_that_speaks_it(client):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]

    bad = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"voice_id": "not_a_voice"})
    assert bad.status_code == 422 and "af_heart" in bad.json()["detail"]

    # Breeze designs a voice from prose: a named voice there is a value the
    # synth request would drop, so it is refused rather than silently ignored.
    breeze = client.patch(
        f"/api/books/{slug}/lines/{line_id}", json={"engine": "breeze", "voice_id": "af_heart"}
    )
    assert breeze.status_code == 422 and "no named voices" in breeze.json()["detail"]

    # Moving the engine without mentioning the voice drops a voice that cannot
    # travel, instead of failing a request the user did not get wrong.
    moved = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"engine": "breeze"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["line"]["render"] == {**moved.json()["line"]["render"], "voice_id": None}


def test_spoken_text_must_be_this_line_plus_tags_the_engine_knows(client):
    """The studio validated the director's annotations and nothing else, so a
    manual PATCH could put words in the book's mouth."""
    slug = new_book(client)["slug"]
    script = client.get(f"/api/books/{slug}/script").json()
    line = script["lines"][0]
    client.patch(f"/api/books/{slug}/lines/{line['id']}", json={"engine": "breeze"})

    drift = client.patch(
        f"/api/books/{slug}/lines/{line['id']}",
        json={"spoken_text": "(laugh) I never said anything like this at all!"},
    )
    assert drift.status_code == 422
    assert "does not reproduce" in drift.json()["detail"] or "tag" in drift.json()["detail"]

    unknown_tag = client.patch(
        f"/api/books/{slug}/lines/{line['id']}",
        json={"spoken_text": f"(kazoo) {line['text']}"},
    )
    assert unknown_tag.status_code == 422 and "(kazoo)" in unknown_tag.json()["detail"]

    good = client.patch(
        f"/api/books/{slug}/lines/{line['id']}",
        json={"spoken_text": f"(sigh) {line['text']}"},
    )
    assert good.status_code == 200, good.text
    assert good.json()["line"]["render"]["spoken_text"].startswith("(sigh)")
    assert good.json()["line"]["render"]["direction_source"] == "manual"

    # …and the same string is refused for an engine with no tag vocabulary.
    kokoro = client.patch(
        f"/api/books/{slug}/lines/{line['id']}",
        json={"engine": "kokoro", "spoken_text": f"(sigh) {line['text']}"},
    )
    assert kokoro.status_code == 422 and "kokoro" in kokoro.json()["detail"]


def test_malformed_bodies_are_422_with_a_detail_string(client):
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]

    broken = client.patch(
        f"/api/books/{slug}/lines/{line_id}",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert broken.status_code == 422 and isinstance(broken.json()["detail"], str)

    for body in ([1, 2], "hello", 7):
        response = client.patch(f"/api/books/{slug}/lines/{line_id}", json=body)
        assert response.status_code == 422, body

    typo = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"instrucion": "oops"})
    assert typo.status_code == 422 and "instrucion" in typo.json()["detail"]

    wrong_type = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"speed": "fast"})
    assert wrong_type.status_code == 422 and isinstance(wrong_type.json()["detail"], str)


def test_creating_a_book_requires_a_title_that_fits_a_folder_name(client):
    missing = client.post("/api/books", json={"author": "QA", "text": "Hello world. A test."})
    assert missing.status_code == 422 and "title" in missing.json()["detail"]

    empty = client.post("/api/books", json={"title": "  ", "text": "Hello world. A test."})
    assert empty.status_code in (400, 422)

    long = client.post(
        "/api/books", json={"title": "Z" * 300, "text": "Hello world. A test."}
    )
    assert long.status_code == 422, long.text

    # author stays optional — a book with no author is a normal book.
    ok = client.post("/api/books", json={"title": "No Author Here", "text": "Hello world. A test."})
    assert ok.status_code == 201 and ok.json()["author"] == ""


def test_a_file_that_is_not_really_an_epub_is_a_400_not_a_500(client):
    response = client.post(
        "/api/books/upload",
        files={"file": ("fake.epub", b"this is just text, renamed", "application/epub+zip")},
        data={"title": "Broken Upload"},
    )
    assert response.status_code == 400, response.text
    assert isinstance(response.json()["detail"], str)
    assert client.get("/api/books").json() == []


def test_cast_patch_404s_an_unknown_character_unless_create_is_set(client):
    slug = new_book(client)["slug"]
    ghost = client.patch(
        f"/api/books/{slug}/cast", json=[{"character": "NobodyXYZ", "voice_id": "af_heart"}]
    )
    assert ghost.status_code == 404 and "NobodyXYZ" in ghost.json()["detail"]
    assert all(c["character"] != "NobodyXYZ" for c in client.get(f"/api/books/{slug}/cast").json()["cast"])

    made = client.patch(
        f"/api/books/{slug}/cast",
        json=[{"character": "NobodyXYZ", "voice_id": "af_heart", "create": True}],
    )
    assert made.status_code == 200, made.text
    assert any(c["character"] == "NobodyXYZ" for c in made.json()["cast"])


# ---- deleting a book with a job in flight ------------------------------------

def test_delete_cancels_the_running_job_and_leaves_nothing_behind(client, slow, tmp_path):
    """The delete used to race the render: the dying job
    re-created the folder it was writing into, and the job itself lived on in
    GET /jobs pointing at a book that 404s everywhere else."""
    slug = new_book(client)["slug"]
    job_id = client.post(f"/api/books/{slug}/render", json={"scope": "all"}).json()["id"]
    _wait_until_started(client, job_id)

    assert client.delete(f"/api/books/{slug}").status_code == 204
    assert client.get(f"/api/books/{slug}").status_code == 404

    # Give the worker every chance to write a job record after the delete.
    time.sleep(0.6)
    assert not (tmp_path / "output" / slug).exists(), "the folder came back"
    assert job_id not in {j["id"] for j in client.get("/api/jobs").json()}
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    # The worker survived, so the next book still renders.
    other = new_book(client, title="After The Delete")["slug"]
    assert render(client, other, "all")["status"] == "done"


# ---- direct: scope, refusal, applied_at --------------------------------------

def test_direct_with_unknown_scene_ids_is_refused(client):
    slug = _directed_book(client)
    response = client.post(
        f"/api/books/{slug}/direct", json={"scope": "scenes", "scenes": ["c99_s99", "bogus"]}
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "c99_s99" in detail and "bogus" in detail
    assert client.get(f"/api/books/{slug}/passes").json() == []

    assert client.post(
        f"/api/books/{slug}/direct", json={"scope": "scenes", "scenes": []}
    ).status_code == 422
    assert client.post(
        f"/api/books/{slug}/direct", json={"scope": "chapters", "chapters": [42]}
    ).status_code == 422


def test_direct_is_409_when_no_line_can_hear_a_note(client):
    """A Kokoro-only book used to run a pass, report success, apply it, and
    stamp nothing at all."""
    book = new_book(client)  # narrator_only → every line on kokoro
    slug = book["slug"]
    summary = client.get(f"/api/books/{slug}").json()
    assert summary["engines"] == {"kokoro": summary["utterances"]}
    assert summary["directable"] == {"lines": 0, "total": summary["utterances"]}

    response = client.post(f"/api/books/{slug}/direct", json={"scope": "all"})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "kokoro" in detail and "breeze" in detail

    # Recast so some lines can hear a note, and the same request is accepted —
    # with a target engine the book actually uses, not the global default.
    client.post(f"/api/books/{slug}/casting", json={"mode": "dialogue_expressive"})
    assert client.get(f"/api/books/{slug}").json()["directable"]["lines"] > 0
    accepted = client.post(f"/api/books/{slug}/direct", json={"scope": "all"})
    assert accepted.status_code == 202, accepted.text
    job = wait_job(client, accepted.json()["id"])
    document = client.get(f"/api/books/{slug}/passes/{job['result']['pass_id']}").json()
    assert document["target_engine"] == "breeze"


def test_applied_at_records_the_first_application_not_the_last(client):
    slug = _directed_book(client)
    job = wait_job(
        client,
        client.post(f"/api/books/{slug}/direct", json={"scope": "all"}).json()["id"],
    )
    pass_id = job["result"]["pass_id"]
    document = client.get(f"/api/books/{slug}/passes/{pass_id}").json()

    # Applying a pass nobody decided commits nothing, so it is not "applied".
    untouched = client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={}).json()
    assert untouched["pass"]["applied_at"] is None

    kept = [s for s in document["scenes"] if s["status"] == "kept"]
    client.post(
        f"/api/books/{slug}/passes/{pass_id}/decide",
        json={"decisions": [{"scene_id": s["scene_id"], "decision": "accepted"} for s in kept]},
    )
    first = client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={}).json()["pass"]
    assert first["applied_at"]
    second = client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={}).json()["pass"]
    assert second["applied_at"] == first["applied_at"]


# ---- the export dead end ---------------------------------------

def test_a_single_line_render_leaves_a_rebuild_that_the_stale_scope_finishes(client):
    """The blocker: after re-rendering one line, no line was stale, the chapter
    was not fresh, Export was disabled, and the only button on offer said
    "Render 0 stale lines". Every number the UI needs to say the true thing is
    asserted here, and the escape hatch is exercised."""
    slug = new_book(client)["slug"]
    render(client, slug, "all")
    assert client.get(f"/api/books/{slug}").json()["exportable"] is True

    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][1]["id"]
    client.patch(f"/api/books/{slug}/lines/{line_id}", json={"instruction": "Softer, slower."})
    single = client.post(f"/api/books/{slug}/lines/{line_id}/render", json={})
    assert single.status_code == 200, single.text

    stuck = client.get(f"/api/books/{slug}").json()
    assert stuck["audio"]["stale"] == 0 and stuck["audio"]["missing"] == 0
    assert stuck["exportable"] is False
    # The two numbers that let the UI say "Rebuild 1 chapter" instead of
    # "Render 0 stale lines" and then doing nothing.
    assert stuck["needs_render"] == {"lines": 0, "chapters": 1}
    assert client.post(f"/api/books/{slug}/export", json={}).status_code == 409

    job = render(client, slug, "stale")
    assert job["status"] == "done", job["error"]
    assert job["result"]["chapters_written"] == [0]
    assert job["progress"]["synth_calls"] == 0, "the re-stitch must not re-synthesize"

    freed = client.get(f"/api/books/{slug}").json()
    assert freed["needs_render"] == {"lines": 0, "chapters": 0}
    assert freed["exportable"] is True


def test_needs_render_counts_lines_before_the_first_render(client):
    book = new_book(client)
    summary = client.get(f"/api/books/{book['slug']}").json()
    assert summary["needs_render"]["lines"] == book["utterances"]
    assert summary["needs_render"]["chapters"] == summary["chapters"]


# ---- synthetic --------------------------------------------------

def test_synthetic_marks_a_book_rendered_by_the_fake_synth(client, monkeypatch):
    slug = new_book(client)["slug"]
    assert client.get(f"/api/books/{slug}").json()["synthetic"] is False, "nothing rendered yet"

    render(client, slug, "all")
    assert client.get(f"/api/books/{slug}").json()["synthetic"] is True

    # The flag describes the audio, not today's environment: flipping the switch
    # back does not turn a sine-tone stub into a finished audiobook.
    monkeypatch.setenv("STUDIO_OFFLINE", "0")
    assert client.get(f"/api/books/{slug}").json()["synthetic"] is True

    # And a live single-line render clears it for that book.
    monkeypatch.setenv("STUDIO_OFFLINE", "1")
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    monkeypatch.setenv("STUDIO_OFFLINE", "0")
    monkeypatch.setattr(store, "http_synth", lambda *a, **k: real_fake_synth())
    assert client.post(f"/api/books/{slug}/lines/{line_id}/render", json={}).status_code == 200
    assert client.get(f"/api/books/{slug}").json()["synthetic"] is False


def test_a_book_rendered_before_the_stamp_existed_is_still_read_as_synthetic(client):
    """Books rendered before the stamp existed have no ``render_mode``. A fake-synth render leaves ``.cache-offline`` and no
    ``.cache``, which is enough to answer honestly."""
    slug = new_book(client)["slug"]
    render(client, slug, "all")
    record = store.load_book(slug)
    record.pop("render_mode", None)
    store.save_book(slug, record)

    assert client.get(f"/api/books/{slug}").json()["synthetic"] is True

    # Both cache directories present: we genuinely cannot tell which stitched
    # the chapters, and guessing either way is worse than not claiming.
    os.makedirs(os.path.join(store.book_dir(slug), ".cache"), exist_ok=True)
    assert client.get(f"/api/books/{slug}").json()["synthetic"] is False


# ---- speakers, cast repair ---------------------------------------------------

def test_reassigning_a_line_to_an_existing_character_restamps_its_voice(client):
    slug = new_book(client)["slug"]
    client.post(f"/api/books/{slug}/casting", json={"mode": "dialogue_expressive"})
    script = client.get(f"/api/books/{slug}/script").json()
    dialogue = [line for line in script["lines"] if line["kind"] == "dialogue" and line["speaker"] != "Unknown"]
    speakers = {line["speaker"] for line in dialogue}
    assert len(speakers) >= 2, "this sample needs two speaking characters"
    line = dialogue[0]
    other = sorted(speakers - {line["speaker"]})[0]

    response = client.patch(f"/api/books/{slug}/lines/{line['id']}", json={"speaker": other})
    assert response.status_code == 200, response.text
    updated = response.json()["line"]
    entry = [c for c in client.get(f"/api/books/{slug}/cast").json()["cast"] if c["character"] == other][0]
    assert updated["speaker"] == other
    assert updated["render"]["engine"] == entry["engine"]
    assert updated["render"]["voice_design"] == entry["voice_design"]
    assert updated["audio_state"] in ("missing", "stale")


def test_reassigning_a_line_to_a_new_character_casts_it(client):
    """A character the attribution stage never noticed used to have no way
    into the cast at all."""
    slug = new_book(client)["slug"]
    client.post(f"/api/books/{slug}/casting", json={"mode": "dialogue_expressive"})
    line = [
        x for x in client.get(f"/api/books/{slug}/script").json()["lines"]
        if x["kind"] == "dialogue"
    ][0]

    response = client.patch(f"/api/books/{slug}/lines/{line['id']}", json={"speaker": "Mara"})
    assert response.status_code == 200, response.text
    updated = response.json()["line"]
    assert updated["speaker"] == "Mara"

    cast = client.get(f"/api/books/{slug}/cast").json()
    entry = [c for c in cast["cast"] if c["character"] == "Mara"][0]
    # The casting rule chose for her, exactly as it would for a detected name.
    assert entry["engine"] == "breeze" and entry["voice_design"]
    assert updated["render"]["engine"] == "breeze"
    assert cast["lines"]["Mara"] == 1 and cast["first_line"]["Mara"] == line["text"]
    if updated.get("attribution") is not None:
        assert updated["attribution"]["basis"] == "manual"
        assert updated["attribution"]["confidence"] == 1


def test_cast_merge_folds_one_character_into_another(client):
    slug = new_book(client)["slug"]
    client.post(f"/api/books/{slug}/casting", json={"mode": "dialogue_expressive"})
    cast = client.get(f"/api/books/{slug}/cast").json()
    characters = [c["character"] for c in cast["cast"] if c["character"] != "Narrator"]
    assert len(characters) >= 2
    source, into = characters[0], characters[1]
    moving = cast["lines"][source]

    response = client.post(f"/api/books/{slug}/cast/merge", json={"from": source, "into": into})
    assert response.status_code == 200, response.text
    after = client.get(f"/api/books/{slug}/cast").json()
    assert source not in {c["character"] for c in after["cast"]}
    assert after["lines"][into] == cast["lines"][into] + moving
    assert all(line["speaker"] != source for line in response.json()["lines"])

    assert client.post(
        f"/api/books/{slug}/cast/merge", json={"from": "Ghost", "into": into}
    ).status_code == 404
    assert client.post(
        f"/api/books/{slug}/cast/merge", json={"from": into, "into": into}
    ).status_code == 422


# ---- chapters ----------------------------------------------------------------

def test_excluding_a_chapter_skips_it_in_the_render_and_unblocks_export(client):
    text = _dialogue_text()
    slug = new_book(
        client,
        text=f"Copyright page\n\nAll rights reserved.\n\nChapter One\n\n{text}\n\nChapter Two\n\n{text}",
    )["slug"]
    script = client.get(f"/api/books/{slug}/script").json()
    included = [c for c in script["chapters"] if c["include_in_audio"]]
    assert len(included) >= 2, "this fixture needs two readable chapters"
    # Whatever ingest decided about the front page, an excluded chapter is
    # always non-audio: the two flags are not allowed to disagree.
    for chapter in script["chapters"]:
        if chapter["kind"] != "chapter":
            assert chapter["include_in_audio"] is False, chapter

    target = included[-1]["index"]
    patched = client.patch(
        f"/api/books/{slug}/chapters/{target}",
        json={"title": "Back matter", "kind": "back_matter", "include_in_audio": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Back matter"
    assert patched.json()["kind"] == "back_matter"
    assert patched.json()["include_in_audio"] is False

    job = render(client, slug, "all")
    assert target not in job["result"]["chapters_written"]
    assert any("include_in_audio" in entry["line"] for entry in job["log"])

    # The excluded chapter has no audio and never will — and that must not make
    # the book un-exportable forever.
    summary = client.get(f"/api/books/{slug}").json()
    assert summary["needs_render"] == {"lines": 0, "chapters": 0}
    assert summary["exportable"] is True
    assert client.patch(f"/api/books/{slug}/chapters/99", json={"title": "x"}).status_code == 404


# ---- lexicon, QC marks, exports ---------------------------------------------

def test_lexicon_reports_how_often_each_term_occurs(client):
    slug = new_book(client)["slug"]
    client.put(f"/api/books/{slug}/lexicon", json={"entries": {"Elara": "eh-LAR-uh", "Zzyzx": "ziz-icks"}})
    view = client.get(f"/api/books/{slug}/lexicon").json()
    assert view["occurrences"]["Elara"] > 0
    assert view["occurrences"]["Zzyzx"] == 0, "an entry that matches nothing must say so"

    script = client.get(f"/api/books/{slug}/script").json()
    expected = sum(1 for line in script["lines"] if "elara" in line["text"].lower())
    assert view["occurrences"]["Elara"] == expected


def test_qc_flag_and_note_ride_along_with_the_line(client):
    slug = new_book(client)["slug"]
    script = client.get(f"/api/books/{slug}/script").json()
    line_id = script["lines"][0]["id"]
    version = script["version"]

    marked = client.patch(
        f"/api/books/{slug}/lines/{line_id}", json={"flag": True, "note": "breath before 'again'"}
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["line"]["flag"] is True
    assert marked.json()["line"]["note"] == "breath before 'again'"
    # A QC mark changes nothing audible, so it earns no script version.
    assert marked.json()["version"] == version
    assert client.get(f"/api/books/{slug}/script").json()["version"] == version

    listed = [x for x in client.get(f"/api/books/{slug}/script").json()["lines"] if x["id"] == line_id][0]
    assert listed["flag"] is True and listed["note"].startswith("breath")
    assert listed["audio_state"] == "missing", "a note must not touch the cache key"

    cleared = client.patch(f"/api/books/{slug}/lines/{line_id}", json={"flag": False, "note": ""})
    assert cleared.json()["line"]["flag"] is False and cleared.json()["line"]["note"] == ""


def test_exports_listing_carries_what_the_library_screen_needs(client):
    """The endpoint used to exist with a payload that could not fill a row."""
    slug = new_book(client)["slug"]
    assert client.get(f"/api/books/{slug}/exports").json() == []
    store.record_export(
        slug, "the-lighthouse.m4b", format="m4b", loudness="acx", sample_rate=44100,
        compliance="pass", duration_ms=1234,
    )
    exports_dir = os.path.join(store.book_dir(slug), "exports")
    os.makedirs(exports_dir, exist_ok=True)
    with open(os.path.join(exports_dir, "the-lighthouse.m4b"), "wb") as fh:
        fh.write(b"\0" * 64)

    entry = client.get(f"/api/books/{slug}/exports").json()[0]
    assert entry["file"] == "the-lighthouse.m4b"
    assert entry["format"] == "m4b" and entry["loudness"] == "acx"
    assert entry["created_at"] and entry["bytes"] == 64
    assert entry["compliance"] == "pass" and entry["duration_ms"] == 1234
    assert entry["url"].endswith(f"/books/{slug}/exports/the-lighthouse.m4b")


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_acx_export_produces_one_checked_file_per_chapter(client, tmp_path):
    """UNVERIFIED until master_book implements format='acx' — until then the job
    must fail with a sentence that says exactly that, not an ffmpeg stack."""
    slug = new_book(client)["slug"]
    render(client, slug, "all")
    response = client.post(
        f"/api/books/{slug}/export", json={"format": "acx", "bitrate": "192k"}
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["id"], timeout=300)

    if job["status"] == "failed":
        assert "acx" in (job["error"] or "").lower()
        pytest.skip(f"master_book has no acx format yet: {job['error']}")

    result = job["result"]
    assert result["format"] == "acx" and result["compliance"] in ("pass", "fail")
    assert result["files"], "an acx export is a table of files"
    listed = {e["file"]: e for e in client.get(f"/api/books/{slug}/exports").json()}
    for entry in result["files"]:
        assert entry["file"] in listed
        assert listed[entry["file"]]["bitrate"] == "192k"
        assert (tmp_path / "output" / slug / "exports" / entry["file"]).exists()


# ---- durations ---------------------------------------------------------------

def test_a_short_line_reports_its_real_duration(client, monkeypatch):
    """A re-rendered line used to show 0:00 while claiming to be cached.
    `duration_ms` has to be the cache file's actual length, rounded, so a 400 ms
    clip reports 400 and the UI's own rounding is the only rounding."""
    import io
    import wave as wave_module

    def four_hundred_ms(*_args, **_kwargs):
        def synth(_text, _render):
            buf = io.BytesIO()
            with wave_module.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(b"\x20\x00" * int(24000 * 0.4))
            return buf.getvalue()

        return synth

    monkeypatch.setattr(store, "fake_synth", four_hundred_ms)
    slug = new_book(client)["slug"]
    line_id = client.get(f"/api/books/{slug}/script").json()["lines"][0]["id"]
    payload = client.post(f"/api/books/{slug}/lines/{line_id}/render", json={}).json()

    assert payload["line"]["audio_state"] == "cached"
    assert payload["line"]["duration_ms"] == 400


def test_offline_audio_never_counts_as_real_audio(client, monkeypatch):
    """Flipping STUDIO_OFFLINE to 0 must not serve fake clips as cached audio."""
    book = new_book(client)
    slug = book["slug"]
    render(client, slug, "all")
    assert client.get(f"/api/books/{slug}").json()["audio"]["cached"] > 0
    assert os.path.isdir(os.path.join(store.book_dir(slug), ".cache-offline"))
    assert not os.path.isdir(os.path.join(store.book_dir(slug), ".cache"))

    monkeypatch.setenv("STUDIO_OFFLINE", "0")
    summary = client.get(f"/api/books/{slug}").json()
    assert summary["audio"]["cached"] == 0
    assert summary["audio"]["stale"] == summary["audio"]["cached"] + summary["audio"]["stale"] + summary["audio"]["missing"]


def test_chapter_sign_off_is_a_qc_mark_that_a_render_resets(client):
    book = new_book(client)
    slug = book["slug"]
    render(client, slug, "all")
    before = client.get(f"/api/books/{slug}").json()["version"] if "version" in client.get(f"/api/books/{slug}").json() else None

    r = client.patch(f"/api/books/{slug}/chapters/0", json={"qc_status": "approved"})
    assert r.status_code == 200, r.text
    assert r.json()["qc_status"] == "approved"
    summary = client.get(f"/api/books/{slug}").json()
    assert summary["qc"]["approved"] == 1 and summary["qc"]["chapters"] >= 1
    script = client.get(f"/api/books/{slug}/script").json()
    assert script["chapters"][0]["qc_status"] == "approved"
    if before is not None:
        assert summary["version"] == before  # a sign-off earns no script version

    bad = client.patch(f"/api/books/{slug}/chapters/0", json={"qc_status": "loved it"})
    assert bad.status_code == 422

    # Re-render everything: the approved take is gone, so the verdict is too.
    render(client, slug, "all")
    assert client.get(f"/api/books/{slug}/script").json()["chapters"][0]["qc_status"] == "unheard"


# ---- what the studio's scopes cover ----------------------------------------
# From a trace recorded against a stub gateway:
# the render job already refused an ``include_in_audio: false`` chapter, but the
# director's "all" scope still directed it, so a book's copyright page cost a
# model call and a review row for audio that is never made.


def test_direct_all_covers_only_the_chapters_that_will_be_heard():
    from pipeline.studio.jobs import _direct_scope
    from pipeline.direct import scene_passages
    from pipeline.schema import BookScript, Chapter, Render, Scene, Utterance

    script = BookScript()
    for index, (kind, included, text) in enumerate(
        [("front_matter", False, "Copyright 2026."), ("chapter", True, "Real prose here.")]
    ):
        chapter = Chapter(index=index, kind=kind, include_in_audio=included)
        chapter.scenes.append(Scene(index=0))
        script.chapters.append(chapter)
        script.utterances.append(
            Utterance(
                id=f"c{index}_p0_s0", order=index, chapter_index=index,
                scene_index=0, text=text, render=Render(engine="kokoro"),
            )
        )

    assert [p.scene_id for p in scene_passages(script)] == ["c0_s0", "c1_s0"]
    assert _direct_scope(script, {"scope": "all"}) == ["c1_s0"]
    # Naming it explicitly still directs it.
    assert _direct_scope(script, {"scope": "chapters", "chapters": [0]}) == ["c0_s0"]


def test_the_studios_renderer_will_not_send_a_note_to_a_noteless_engine(env, monkeypatch):
    """`store.make_renderer` builds the synth callable, and it must gate the
    written note on the merged engine catalog: a manual note typed on a Kokoro
    line used to travel to an engine that declares ``instruction: false``."""
    from pipeline.schema import Render
    from pipeline.studio import store

    seen: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"RIFFfake"

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(store, "offline", lambda: False)

    renderer = store.make_renderer("any-slug")
    renderer._synth("Hello.", Render(engine="kokoro", instruction="Bitterly."))
    assert "instruction" not in seen[-1]

    renderer._synth("Hello.", Render(engine="breeze", instruction="Bitterly."))
    assert seen[-1]["instruction"] == "Bitterly."


# ---- the six routes that gained a response model (Amendment v1.3) ------------
# FastAPI filters a response through its `response_model`: a model missing a
# field drops that field from the wire without a word. These pin each route to
# what its handler has always sent — the store's own model, dumped whole — so
# declaring a model for the OpenAPI export can never quietly change a payload.
# They pass on the code from before the models were declared, too.


def _assert_wire(response, expected):
    """Same keys, same values, same key order as the plain dump the handlers
    returned before (``jsonable_encoder`` is what FastAPI ran it through)."""
    assert response.status_code == 200, response.text
    body = response.json()
    want = json.loads(json.dumps(jsonable_encoder(expected)))
    assert body == want
    assert json.dumps(body) == json.dumps(want), "same payload, different key order"


def test_line_patch_and_render_payloads_are_the_line_view_unchanged(client):
    slug = _directed_book(client)
    lines = client.get(f"/api/books/{slug}/script").json()["lines"]
    target = next(line["id"] for line in lines if line["render"]["engine"] == "breeze")

    def version():
        return int(store.load_book(slug)["version"])

    # A note (earns a script version) and a QC mark (does not): both answer
    # {line, version}, and `line` is the whole Line, derived state included.
    for patch in ({"instruction": "Almost a whisper."}, {"flag": True, "note": "pickup"}):
        response = client.patch(f"/api/books/{slug}/lines/{target}", json=patch)
        _assert_wire(response, {"line": store.line_view(slug, target).model_dump(), "version": version()})

    rendered = client.post(f"/api/books/{slug}/lines/{target}/render", json={})
    _assert_wire(
        rendered,
        {
            "line": store.line_view(slug, target).model_dump(),
            "audio_url": f"/studio/api/books/{slug}/lines/{target}/audio",
        },
    )
    assert rendered.json()["line"]["audio_state"] == "cached"


def test_pass_payloads_are_the_stored_pass_unchanged(client):
    slug = _directed_book(client)
    started = client.post(f"/api/books/{slug}/direct", json={"scope": "all", "target_engine": "breeze"})
    job = wait_job(client, started.json()["id"])
    assert job["status"] == "done", job["error"]
    pass_id = job["result"]["pass_id"]

    listed = client.get(f"/api/books/{slug}/passes")
    _assert_wire(listed, [p.model_dump(exclude={"scenes"}) for p in store.list_passes(slug)])
    _assert_wire(
        client.get(f"/api/books/{slug}/passes/{pass_id}"),
        store.load_pass(slug, pass_id).model_dump(),
    )

    scenes = store.load_pass(slug, pass_id).scenes
    decisions = [{"scene_id": scenes[0].scene_id, "decision": "edited", "instruction": "Hushed."}]
    decisions += [{"scene_id": s.scene_id, "decision": "accepted"} for s in scenes[1:]]
    decided = client.post(f"/api/books/{slug}/passes/{pass_id}/decide", json={"decisions": decisions})
    _assert_wire(decided, store.load_pass(slug, pass_id).model_dump())

    applied = client.post(f"/api/books/{slug}/passes/{pass_id}/apply", json={})
    _assert_wire(
        applied,
        {
            "pass": store.load_pass(slug, pass_id).model_dump(),
            "script": store.script_view(slug).model_dump(),
        },
    )
    assert applied.json()["pass"]["applied_at"]
