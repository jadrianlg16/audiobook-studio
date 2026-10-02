"""Director's Desk additions to the gateway (see docs/STUDIO-API.md, "Gateway
additions"): why a candidate was rejected, the prompt version that produced a
result, and the list of models Ollama actually has.

``test_director.py`` covers the validation rule itself. This file covers the
three things a *book-scale* pass needs on top of it:

1. A rejection must survive as data. At one passage you can eyeball what the
   model wrote; at four hundred scenes, "rejected" with the text thrown away is
   unreviewable and unfixable.
2. The prompt is a file anyone can edit, and a book-scale pass caches per scene.
   Without a prompt hash in that key, tuning the prompt and re-running serves
   every scene from cache and looks like the edit did nothing.
3. A model that isn't pulled should be a visible choice, not a 404 discovered
   forty scenes into a book.

Run from the project root:  pytest gateway/tests
"""

from __future__ import annotations

import asyncio
import os

import httpx
from fastapi.testclient import TestClient

from gateway.app.main import create_app
from gateway.app.models import DirectorEngineSpec
from gateway.app.services import director as d

_TAGS = ["(sigh)", "(laugh)", "(clears throat)"]
_SOURCE = "She put the letter down. It was over, and she had known for weeks."


# ---- why a candidate was rejected -------------------------------------------
# A model that paraphrases needs a different prompt fix than one that invents
# tags, and one that truncates is usually a context-window problem. Collapsing
# all three into "rejected" hides which one you have.

def test_a_kept_annotation_reports_kept_with_no_rejected_text() -> None:
    v = d.inspect_tagged_text(f"(sigh) {_SOURCE}", _SOURCE, _TAGS)
    assert v.status == "kept"
    assert v.text is not None and v.rejected_text is None


def test_a_paraphrase_keeps_the_models_text_for_the_diff() -> None:
    """The whole point of the new field: the reviewer sees what the model wrote."""
    candidate = "(sigh) She set the letter down. It was finished, and she had known for weeks."
    v = d.inspect_tagged_text(candidate, _SOURCE, _TAGS)
    assert v.status == "rejected"
    assert v.text is None
    assert v.rejected_text is not None and "She set the letter down" in v.rejected_text
    assert v.reason and "not the source passage" in v.reason


def test_an_unknown_tag_is_named_in_the_reason() -> None:
    v = d.inspect_tagged_text(f"(whistles) {_SOURCE}", _SOURCE, _TAGS)
    assert v.status == "rejected"
    assert v.reason and "does not know" in v.reason and "(whistles)" in v.reason


def test_a_truncation_says_how_much_came_back() -> None:
    v = d.inspect_tagged_text("(sigh) She put the letter down.", _SOURCE, _TAGS)
    assert v.status == "rejected"
    assert v.reason and v.reason.startswith("truncated")


def test_no_tag_vocabulary_and_no_tags_added_are_both_none_not_rejected() -> None:
    """Neither is the model changing the book, so neither may inflate the rejected
    count — that number is the one the prompt is tuned against."""
    assert d.inspect_tagged_text(f"(sigh) {_SOURCE}", _SOURCE, []).status == "none"
    assert d.inspect_tagged_text(_SOURCE, _SOURCE, _TAGS).status == "none"


def test_rejected_text_is_canonicalized_so_the_diff_shows_our_tag_spelling() -> None:
    v = d.inspect_tagged_text(
        "[sigh] She set the letter down. It was finished, and she had known for weeks.",
        _SOURCE,
        _TAGS,
    )
    assert v.status == "rejected"
    assert v.rejected_text is not None and v.rejected_text.startswith("(sigh)")


def test_per_engine_carries_status_and_rejected_text() -> None:
    engines = [DirectorEngineSpec(id="breeze", instruction=True, audio_tags=_TAGS)]
    raw = [{"engine_id": "breeze", "instruction": "Slow.", "text": f"(sigh) {_SOURCE} And more."}]
    entry = d._validate_per_engine(raw, engines, 0.5, _SOURCE)[0]

    assert entry["text"] is None
    assert entry["text_status"] == "rejected"
    assert entry["rejected_text"] is not None
    assert entry["text_reason"]
    # The instruction survives a text rejection — that is exactly the "keep the
    # note, keep the book's words" review option (PassScene decision kept_text).
    assert entry["instruction"] == "Slow."


def test_engine_without_tags_reports_none_not_rejected() -> None:
    engines = [DirectorEngineSpec(id="kokoro", voices=["af_heart"])]
    entry = d._validate_per_engine(
        [{"engine_id": "kokoro", "text": f"(sigh) {_SOURCE}"}], engines, 0.5, _SOURCE
    )[0]
    assert entry["text_status"] == "none" and entry["rejected_text"] is None


# ---- prompt version ---------------------------------------------------------

def test_prompt_version_is_stable_and_tracks_the_prompt_body(tmp_path, monkeypatch) -> None:
    """It keys a book-scale cache: a re-save must not invalidate it, a real edit must."""
    variant = tmp_path / "director.md"
    variant.write_text("docs\n---\nVERSION ONE {{TEXT}}", encoding="utf-8")
    monkeypatch.setenv("DIRECTOR_PROMPT_PATH", str(variant))
    monkeypatch.setattr(d, "_prompt_cache", None)
    first = d.prompt_version()
    assert len(first) == 12

    variant.write_text("docs\n---\nVERSION ONE {{TEXT}}", encoding="utf-8")  # same body
    os.utime(variant, (0, 0))  # distinct mtime, identical content
    assert d.prompt_version() == first  # a re-save keeps every cached scene

    variant.write_text("docs\n---\nVERSION TWO {{TEXT}}", encoding="utf-8")
    os.utime(variant, (1, 1))
    assert d.prompt_version() != first  # a real edit costs a re-run


def test_prompt_version_is_empty_when_the_prompt_is_missing(tmp_path, monkeypatch) -> None:
    """A broken deployment must still answer /director/defaults."""
    monkeypatch.setenv("DIRECTOR_PROMPT_PATH", str(tmp_path / "gone.md"))
    monkeypatch.setattr(d, "_prompt_cache", None)
    assert d.prompt_version() == ""


def test_defaults_endpoint_exposes_the_prompt_version() -> None:
    body = TestClient(create_app()).get("/api/director/defaults").json()
    assert body["prompt_version"] == d.prompt_version()
    assert body["prompt_version"]


# ---- GET /api/director/models ----------------------------------------------
# Never touches the network: httpx.AsyncClient is replaced wholesale.

class _FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )


def _patch_httpx(monkeypatch, *, payload=None, status: int = 200, raise_exc=None) -> dict:
    seen: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw) -> None:
            seen["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse(payload, status)

    monkeypatch.setattr("gateway.app.routers.director.httpx.AsyncClient", _FakeClient)
    return seen


def test_models_endpoint_maps_ollamas_tag_list(monkeypatch) -> None:
    seen = _patch_httpx(
        monkeypatch,
        payload={
            "models": [
                {"name": "gemma3:12b", "size": 8_149_190_253, "modified_at": "2026-08-30T10:00:00Z"},
                {"name": "qwen3:30b", "size": 18_000_000_000, "modified_at": "2026-09-01T10:00:00Z"},
            ]
        },
    )
    body = TestClient(create_app()).get("/api/director/models").json()

    assert seen["url"].endswith("/api/tags")
    assert body["provider"] == "ollama" and body["error"] is None
    assert [m["name"] for m in body["models"]] == ["gemma3:12b", "qwen3:30b"]
    assert body["models"][0]["size_bytes"] == 8_149_190_253  # Ollama's field is `size`
    assert body["models"][0]["modified_at"] == "2026-08-30T10:00:00Z"
    assert body["default"]  # whatever DIRECTOR_MODEL says


def test_models_endpoint_returns_200_with_an_error_when_ollama_is_down(monkeypatch) -> None:
    """A blank dropdown with no explanation is the bug this prevents."""
    _patch_httpx(monkeypatch, raise_exc=httpx.ConnectError("connection refused"))
    resp = TestClient(create_app()).get("/api/director/models")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == []
    assert body["error"] and "could not reach Ollama" in body["error"]
    assert body["default"]  # the UI can still show what it *wanted* to use


def test_models_endpoint_survives_an_http_error(monkeypatch) -> None:
    _patch_httpx(monkeypatch, status=500, payload={})
    body = TestClient(create_app()).get("/api/director/models").json()
    assert body["models"] == [] and "HTTP 500" in body["error"]


def test_models_endpoint_skips_junk_entries_instead_of_500ing(monkeypatch) -> None:
    _patch_httpx(
        monkeypatch,
        payload={"models": [{"nope": 1}, "junk", {"model": "llama3.2:latest", "size": "big"}]},
    )
    body = TestClient(create_app()).get("/api/director/models").json()
    assert [m["name"] for m in body["models"]] == ["llama3.2:latest"]
    assert body["models"][0]["size_bytes"] == 0  # an unparseable size degrades, not 500


def test_models_endpoint_uses_a_short_timeout(monkeypatch) -> None:
    """This is a UI dropdown, not a synthesis call — it must not hang the page."""
    seen = _patch_httpx(monkeypatch, payload={"models": []})
    TestClient(create_app()).get("/api/director/models")
    assert seen["timeout"] is not None and seen["timeout"] <= 10.0


def test_annotate_stamps_the_prompt_version(monkeypatch) -> None:
    """A pass records the prompt each scene was directed with."""
    from gateway.app.models import DirectorRequest

    async def fake_ollama(model, prompt, base_url):
        return '{"summary": "s", "emotion": "sad", "scalar": 0.4, "per_engine": []}'

    monkeypatch.setattr(d, "_ollama", fake_ollama)
    req = DirectorRequest(text=_SOURCE, model="gemma3:12b", engines=[])
    data = asyncio.run(d.annotate(req, "http://ollama", ""))
    assert data["prompt_version"] == d.prompt_version()
