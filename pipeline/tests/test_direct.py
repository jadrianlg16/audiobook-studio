"""Book-scale director pass — fully offline (a fake annotate, no LLM, no network).

The tests are grouped by the thing that can silently ship a wrong audiobook:

- **grouping**: the model must see a scene as prose, not as one sentence per line.
- **distribution**: an annotated scene has to come apart onto the exact
  utterances it came from. A tag on the wrong line is a wrong take that sounds
  completely fine, so every line is re-checked and a scene that will not line up
  is rejected whole.
- **rejection**: a paraphrase must be kept as data, not just counted.
- **resumability**: checkpoint per scene, a cache that makes a re-run free, and a
  cooperative stop — the three things that decide whether a 400-scene book is
  survivable.
- **apply**: a note a person typed is never overwritten, and a note never lands
  on an engine that cannot use one.

Run from the project root:  pytest pipeline/tests
"""

from __future__ import annotations

import pytest

from pipeline.direct import (
    Pass,
    PassScene,
    TagDistributionError,
    apply_pass,
    direct_scene,
    distribute_tags,
    fake_annotate,
    instruction_is_inert,
    retry_scenes,
    run_pass,
    scene_cache_key,
    scene_passages,
)
from pipeline.profiles import get_profile
from pipeline.schema import BookScript, Chapter, Render, Scene, Utterance, UtteranceKind

_TAGS = ["(sigh)", "(laugh)", "(pause)"]
_SPEC = {
    "id": "breeze",
    "name": "Breeze TTS 2",
    "audio_tags": _TAGS,
    "instruction": True,
}
_NO_TAG_SPEC = {"id": "indextts2", "name": "IndexTTS 2", "audio_tags": [], "instruction": True}


# ---- fixtures ---------------------------------------------------------------

def _script(rows, *, engine: str = "breeze") -> BookScript:
    """Build a script from ``(id, text, chapter, scene)`` rows.

    Hand-built rather than run through ``build.py`` so the exact wording, and
    therefore the exact tag placement, is under the test's control.
    """
    script = BookScript()
    chapters: dict[int, Chapter] = {}
    for order, (uid, text, ci, si) in enumerate(rows):
        chapter = chapters.get(ci)
        if chapter is None:
            chapter = chapters[ci] = Chapter(index=ci, title=f"Chapter {ci + 1}")
            script.chapters.append(chapter)
        if not any(s.index == si for s in chapter.scenes):
            chapter.scenes.append(Scene(index=si))
        script.utterances.append(
            Utterance(
                id=uid,
                order=order,
                chapter_index=ci,
                scene_index=si,
                speaker="Narrator",
                text=text,
                render=Render(engine=engine),
            )
        )
    return script


_ROWS = [
    ("c0_p0_s0", "Alpha here.", 0, 0),
    ("c0_p0_s1", "Beta too.", 0, 0),
    ("c0_p1_s0", "A second paragraph.", 0, 0),
    ("c0_p2_s0", "Gamma alone.", 0, 1),
    ("c1_p0_s0", "Omega.", 1, 0),
]


def _passage(script: BookScript, scene_id: str = "c0_s0"):
    return next(p for p in scene_passages(script) if p.scene_id == scene_id)


def _stub_annotate(**entry):
    """An annotate that returns one fixed per-engine entry."""
    base = {
        "engine_id": "breeze",
        "instruction": None,
        "text": None,
        "rejected_text": None,
        "text_status": "none",
        "text_reason": None,
    }
    base.update(entry)

    def annotate(text, specs):
        return {"summary": "s", "emotion": "calm", "scalar": 0.5, "per_engine": [base]}

    return annotate


# ---- grouping ---------------------------------------------------------------

def test_scenes_are_grouped_by_chapter_and_scene_with_stable_ids() -> None:
    passages = scene_passages(_script(_ROWS))
    assert [p.scene_id for p in passages] == ["c0_s0", "c0_s1", "c1_s0"]
    assert [p.chapter_index for p in passages] == [0, 0, 1]
    assert passages[0].line_ids == ["c0_p0_s0", "c0_p0_s1", "c0_p1_s0"]
    assert passages[0].chapter_title == "Chapter 1"


def test_passage_text_rebuilds_paragraphs_not_one_sentence_per_line() -> None:
    """Sentence-per-line text makes the model annotate each line as if it stood
    alone — the drift the scene-level design exists to avoid."""
    text = _passage(_script(_ROWS)).text
    assert text == "Alpha here. Beta too.\n\nA second paragraph."


def test_a_single_line_scene_is_still_a_passage() -> None:
    passages = scene_passages(_script(_ROWS))
    assert passages[2].text == "Omega." and passages[2].line_ids == ["c1_p0_s0"]


# ---- tag distribution -------------------------------------------------------

def test_tag_at_the_very_start_goes_to_the_first_line() -> None:
    passage = _passage(_script(_ROWS))
    got = distribute_tags(passage, "(sigh) Alpha here. Beta too.\n\nA second paragraph.", _TAGS)
    assert got["c0_p0_s0"] == "(sigh) Alpha here."
    assert got["c0_p0_s1"] == "Beta too."


def test_tag_between_sentences_goes_to_the_line_it_precedes() -> None:
    """The prompt asks for tags 'before the words it colours', so a tag in the
    gap belongs to the *next* utterance, not the one that just ended."""
    passage = _passage(_script(_ROWS))
    got = distribute_tags(passage, "Alpha here. (sigh) Beta too.\n\nA second paragraph.", _TAGS)
    assert got["c0_p0_s0"] == "Alpha here."
    assert got["c0_p0_s1"] == "(sigh) Beta too."


def test_tag_at_the_very_end_goes_to_the_last_line() -> None:
    """There is no following line to hand it to, and dropping it would quietly
    lose direction the model asked for."""
    passage = _passage(_script(_ROWS))
    got = distribute_tags(passage, "Alpha here. Beta too.\n\nA second paragraph. (sigh)", _TAGS)
    assert got["c0_p1_s0"] == "A second paragraph. (sigh)"


def test_tag_inside_a_line_stays_where_it_was_put() -> None:
    passage = _passage(_script(_ROWS))
    got = distribute_tags(passage, "Alpha (pause) here. Beta too.\n\nA second paragraph.", _TAGS)
    assert got["c0_p0_s0"] == "Alpha (pause) here."


def test_tag_in_front_of_punctuation_survives_the_per_line_check() -> None:
    """Stripping it leaves 'Alpha here .' — an artifact of our stripping, not a
    change to the author's words. Rejecting that would throw away a good take."""
    passage = _passage(_script(_ROWS))
    got = distribute_tags(passage, "Alpha here (pause). Beta too.\n\nA second paragraph.", _TAGS)
    assert got["c0_p0_s0"] == "Alpha here (pause)."


def test_every_line_gets_an_entry_even_without_a_tag() -> None:
    passage = _passage(_script(_ROWS))
    got = distribute_tags(passage, "(sigh) Alpha here. Beta too.\n\nA second paragraph.", _TAGS)
    assert set(got) == set(passage.line_ids)


def test_multiple_tags_land_on_their_own_lines() -> None:
    passage = _passage(_script(_ROWS))
    got = distribute_tags(
        passage, "Alpha here. (sigh) Beta too.\n\n(laugh) A second paragraph.", _TAGS
    )
    assert got["c0_p0_s1"].startswith("(sigh)")
    assert got["c0_p1_s0"].startswith("(laugh)")


def test_a_changed_word_cannot_be_distributed() -> None:
    """The gateway would have caught this first; the per-line check is the second
    net, and it must not silently 'fix' the mismatch by shifting a boundary."""
    passage = _passage(_script(_ROWS))
    with pytest.raises(TagDistributionError):
        distribute_tags(passage, "Alpha there. Beta too.\n\nA second paragraph.", _TAGS)


def test_extra_text_after_the_last_line_is_refused() -> None:
    passage = _passage(_script(_ROWS))
    with pytest.raises(TagDistributionError):
        distribute_tags(passage, "Alpha here. Beta too.\n\nA second paragraph. And more.", _TAGS)


def test_a_truncated_annotation_is_refused() -> None:
    passage = _passage(_script(_ROWS))
    with pytest.raises(TagDistributionError):
        distribute_tags(passage, "(sigh) Alpha here.", _TAGS)


def test_a_tag_inserted_inside_a_word_is_refused() -> None:
    """This is what the *per-line* strip check is for, and nothing else catches it.

    The character walk that finds the line boundaries ignores whitespace, so
    ``Alp(sigh)ha`` lines up perfectly: the significant characters are still
    ``Alphahere.``. Only stripping the tag back out of the finished line exposes
    it — ``Alp ha here.`` is not ``Alpha here.``, and an engine handed that would
    say "alp ha". Deleting the strip check must fail this test.
    """
    passage = _passage(_script(_ROWS))
    with pytest.raises(TagDistributionError, match="did not reproduce"):
        distribute_tags(passage, "Alp(sigh)ha here. Beta too.\n\nA second paragraph.", _TAGS)


def test_a_word_split_in_two_is_refused() -> None:
    """Same net, without a tag: the walk skips whitespace, the strip check doesn't."""
    passage = _passage(_script(_ROWS))
    with pytest.raises(TagDistributionError, match="did not reproduce"):
        distribute_tags(passage, "(sigh) Al pha here. Beta too.\n\nA second paragraph.", _TAGS)


def test_pipeline_squash_matches_the_gateways() -> None:
    """``_squash`` is duplicated on purpose (the studio image has no gateway
    package). This pins the copy so a drift fails here instead of quietly
    rejecting good annotations in production."""
    gw = pytest.importorskip("gateway.app.services.director")
    from pipeline import direct

    for sample in [
        "Alpha here .",
        "a  b\nc",
        'She said "no" .',
        "again . Outside",
        "one,  two ; three",
    ]:
        assert direct._squash(sample) == gw._squash(sample)


# ---- directing one scene ----------------------------------------------------

def test_a_kept_scene_carries_the_note_the_tags_and_the_per_line_text() -> None:
    annotate = _stub_annotate(
        instruction="Slow and restrained.",
        text="Alpha here. (sigh) Beta too.\n\nA second paragraph.",
        text_status="kept",
    )
    scene = direct_scene(_passage(_script(_ROWS)), annotate, _SPEC, "pv1")

    assert scene.status == "kept" and scene.tags_status == "kept"
    assert scene.instruction == "Slow and restrained."
    assert scene.tags_used == {"(sigh)": 1}
    assert scene.line_spoken_text["c0_p0_s1"].startswith("(sigh)")
    assert scene.attempts == 1
    assert scene.decision == "pending"  # a pass proposes; it never decides
    assert scene.prompt_version == "pv1"


def test_a_paraphrase_rejects_the_scene_and_keeps_the_models_text() -> None:
    """The model's text is the whole value of a rejection: without it the review
    screen can only say 'something was wrong with 37 scenes'."""
    annotate = _stub_annotate(
        instruction="Slow and restrained.",
        rejected_text="(sigh) Alpha there. Beta too.\n\nA second paragraph.",
        text_status="rejected",
        text_reason="not the source passage",
    )
    scene = direct_scene(_passage(_script(_ROWS)), annotate, _SPEC, "pv1")

    assert scene.status == "rejected" and scene.tags_status == "rejected"
    assert scene.model_text is not None and "Alpha there" in scene.model_text
    assert scene.line_spoken_text is None
    assert scene.reason == "not the source passage"
    # The note survives, so the reviewer can take it without the tags.
    assert scene.instruction == "Slow and restrained."


def test_a_scene_that_validates_but_will_not_distribute_is_rejected_whole() -> None:
    """The gateway compares the *scene*; a tag that cannot be pinned to a line
    would otherwise be attached to a guess. A wrong line is a wrong take."""
    script = _script(_ROWS)
    passage = _passage(script)
    passage.lines[1].text = "Beta two."  # line no longer matches the scene text
    annotate = _stub_annotate(text="(sigh) Alpha here. Beta too.\n\nA second paragraph.", text_status="kept")
    scene = direct_scene(passage, annotate, _SPEC, "")

    assert scene.status == "rejected"
    assert scene.tags_status == "rejected"
    assert scene.line_spoken_text is None
    assert scene.reason and "could not be mapped" in scene.reason
    assert scene.model_text is not None  # still diffable


def test_an_instruction_alone_is_kept() -> None:
    scene = direct_scene(
        _passage(_script(_ROWS)), _stub_annotate(instruction="Slow."), _SPEC, ""
    )
    assert scene.status == "kept" and scene.tags_status == "none"
    assert scene.line_spoken_text is None


def test_nothing_usable_is_ignored_not_rejected() -> None:
    """'Ignored' must not inflate the rejected count — that number means the
    model tried to change the book, and it is the one a prompt is tuned against."""
    scene = direct_scene(_passage(_script(_ROWS)), _stub_annotate(), _SPEC, "")
    assert scene.status == "ignored"
    assert scene.reason


def test_a_failed_call_becomes_an_error_scene_and_never_raises() -> None:
    """One unreachable moment must not lose the 300 scenes already directed."""

    def boom(text, specs):
        raise ConnectionError("ollama went away")

    scene = direct_scene(_passage(_script(_ROWS)), boom, _SPEC, "")
    assert scene.status == "error"
    assert scene.error and "ollama went away" in scene.error


def test_an_engine_with_no_tags_still_takes_a_note() -> None:
    annotate = _stub_annotate(engine_id="indextts2", instruction="Slow and restrained.")
    scene = direct_scene(_passage(_script(_ROWS)), annotate, _NO_TAG_SPEC, "")
    assert scene.status == "kept" and scene.tags_status == "none"


# ---- running a pass ---------------------------------------------------------

def _counting(annotate):
    calls = {"n": 0}

    def wrapped(text, specs):
        calls["n"] += 1
        return annotate(text, specs)

    return wrapped, calls


def test_a_pass_covers_every_scene_and_counts_add_up() -> None:
    script = _script(_ROWS)
    pass_ = run_pass(
        script, annotate=fake_annotate(script), target_spec=_SPEC, model="fake-director"
    )
    assert [s.scene_id for s in pass_.scenes] == ["c0_s0", "c0_s1", "c1_s0"]
    counts = pass_.counts
    assert counts.scenes == 3
    assert counts.kept + counts.rejected + counts.ignored + counts.error == 3
    assert pass_.finished_at is not None
    assert pass_.target_engine == "breeze"


def test_the_fake_director_exercises_the_rejection_path() -> None:
    """The path most likely to be wrong must be reachable with no GPU on the box."""
    script = _script(_ROWS)
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)
    rejected = [s for s in pass_.scenes if s.status == "rejected"]
    assert rejected, "the fake director never rejected a scene"
    assert rejected[0].model_text  # the diff is available
    assert rejected[0].instruction  # and the note is still offerable as kept_text


def test_scope_limits_the_pass_to_the_named_scenes() -> None:
    script = _script(_ROWS)
    pass_ = run_pass(script, scenes=["c1_s0"], annotate=fake_annotate(script), target_spec=_SPEC)
    assert [s.scene_id for s in pass_.scenes] == ["c1_s0"]
    assert pass_.scope.scenes == ["c1_s0"]


def test_an_unknown_scene_id_is_skipped_not_fatal() -> None:
    """A UI one script-version behind must not kill the job."""
    script = _script(_ROWS)
    pass_ = run_pass(script, scenes=["c9_s9"], annotate=fake_annotate(script), target_spec=_SPEC)
    assert pass_.scenes == [] and pass_.counts.scenes == 0


def test_the_cache_makes_an_unchanged_re_run_free() -> None:
    script = _script(_ROWS)
    annotate, calls = _counting(fake_annotate(script))
    cache: dict = {}
    kwargs = dict(target_spec=_SPEC, model="m", prompt_version="pv1", cache=cache)

    run_pass(script, annotate=annotate, **kwargs)
    first = calls["n"]
    assert first == 3

    run_pass(script, annotate=annotate, **kwargs)
    assert calls["n"] == first  # every scene served from the cache


def test_the_cache_key_moves_with_the_prompt_and_the_model() -> None:
    """Tuning director.md and re-running must not silently serve the old notes."""
    base = scene_cache_key("passage", "gemma3:12b", "pv1", "breeze", _TAGS)
    assert scene_cache_key("passage", "gemma3:12b", "pv2", "breeze", _TAGS) != base
    assert scene_cache_key("passage", "qwen3:30b", "pv1", "breeze", _TAGS) != base
    assert scene_cache_key("passage.", "gemma3:12b", "pv1", "breeze", _TAGS) != base
    assert scene_cache_key("passage", "gemma3:12b", "pv1", "indextts2", _TAGS) != base
    assert scene_cache_key("passage", "gemma3:12b", "pv1", "breeze", ["(sigh)"]) != base
    # Order of the tag list is not meaning.
    assert scene_cache_key("passage", "gemma3:12b", "pv1", "breeze", list(reversed(_TAGS))) == base


def test_a_changed_scene_costs_exactly_one_call_on_re_run() -> None:
    script = _script(_ROWS)
    annotate, calls = _counting(fake_annotate(script))
    cache: dict = {}
    kwargs = dict(target_spec=_SPEC, model="m", prompt_version="pv1", cache=cache)

    run_pass(script, annotate=annotate, **kwargs)
    first = calls["n"]

    edited = _script(_ROWS)
    edited.utterances[4].text = "Omega, at last."  # the whole of scene c1_s0
    run_pass(edited, annotate=annotate, **kwargs)
    assert calls["n"] == first + 1


def test_checkpoint_is_called_after_every_scene() -> None:
    """A crash at scene 380 of 400 must lose one scene, not 380."""
    script = _script(_ROWS)
    seen: list[int] = []
    run_pass(
        script,
        annotate=fake_annotate(script),
        target_spec=_SPEC,
        checkpoint=lambda p: seen.append(len(p.scenes)),
    )
    assert seen == [1, 2, 3]


def test_should_stop_yields_a_partial_pass_with_the_rest_absent() -> None:
    script = _script(_ROWS)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # allow two scenes through

    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC, should_stop=stop)

    assert [s.scene_id for s in pass_.scenes] == ["c0_s0", "c0_s1"]
    assert pass_.scope.scenes == ["c0_s0", "c0_s1", "c1_s0"]  # scope records the intent
    assert pass_.counts.scenes == 2
    assert pass_.finished_at is None  # resumable, not finished


def test_stopping_before_the_first_scene_directs_nothing() -> None:
    script = _script(_ROWS)
    annotate, calls = _counting(fake_annotate(script))
    pass_ = run_pass(script, annotate=annotate, target_spec=_SPEC, should_stop=lambda: True)
    assert pass_.scenes == [] and calls["n"] == 0


def test_progress_reports_each_scene_with_its_total() -> None:
    script = _script(_ROWS)
    seen: list[tuple[int, int, str]] = []
    run_pass(
        script,
        annotate=fake_annotate(script),
        target_spec=_SPEC,
        on_progress=lambda done, total, scene: seen.append((done, total, scene.scene_id)),
    )
    assert seen == [(1, 3, "c0_s0"), (2, 3, "c0_s1"), (3, 3, "c1_s0")]


# ---- applying ---------------------------------------------------------------

def _directed_pass(script: BookScript, decision: str = "accepted") -> Pass:
    """A pass over ``script`` with every kept scene decided."""
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC, model="fake-director")
    for scene in pass_.scenes:
        if scene.status == "kept":
            scene.decision = decision
    return pass_


def test_apply_stamps_the_note_the_tags_and_the_provenance() -> None:
    script = _script(_ROWS)
    pass_ = _directed_pass(script)
    changed = apply_pass(script, pass_)

    assert changed
    stamped = [u for u in script.utterances if u.render.direction_source == "director"]
    assert stamped
    assert all(u.render.instruction for u in stamped)
    assert pass_.applied_at is not None
    # final_instruction records what actually landed on the book.
    kept = [s for s in pass_.scenes if s.decision == "accepted"]
    assert all(s.final_instruction for s in kept)


def test_apply_leaves_pending_and_rejected_scenes_alone() -> None:
    script = _script(_ROWS)
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)  # all pending
    assert apply_pass(script, pass_) == []
    assert all(u.render.direction_source == "none" for u in script.utterances)


def test_apply_never_overwrites_a_line_a_person_edited() -> None:
    """Semantics rule 4, and the reason ``direction_source`` exists at all.

    The line has to sit in a scene the pass actually *accepted*, otherwise the
    guard is never reached and the test passes for the wrong reason.
    """
    script = _script(_ROWS)
    manual = script.utterances[3]  # c0_p2_s0, in scene c0_s1
    manual.render.instruction = "Mine, typed by hand."
    manual.render.direction_source = "manual"
    pass_ = _directed_pass(script)
    assert pass_.scene("c0_s1").decision == "accepted"  # the guard is live

    changed = apply_pass(script, pass_)
    assert manual.render.instruction == "Mine, typed by hand."
    assert manual.render.spoken_text is None
    assert manual.render.direction_source == "manual"
    assert manual.id not in changed


def test_force_lets_a_re_apply_reclaim_a_manual_line() -> None:
    script = _script(_ROWS)
    script.utterances[3].render.instruction = "Mine, typed by hand."
    script.utterances[3].render.direction_source = "manual"
    pass_ = _directed_pass(script)

    apply_pass(script, pass_, force=True)
    assert script.utterances[3].render.direction_source == "director"
    assert script.utterances[3].render.instruction != "Mine, typed by hand."


def test_apply_only_touches_lines_on_an_instruction_capable_engine() -> None:
    """A note on a Kokoro line changes nothing audible but does change its cache
    key — it would re-render the narrator for no reason."""
    script = _script(_ROWS)
    script.utterances[3].render.engine = "kokoro"
    pass_ = _directed_pass(script)

    apply_pass(script, pass_)
    kokoro = script.utterances[3]
    assert kokoro.render.instruction is None
    assert kokoro.render.direction_source == "none"


def test_apply_accepts_a_wider_set_of_instruction_capable_engines() -> None:
    script = _script(_ROWS)
    script.utterances[3].render.engine = "indextts2"
    pass_ = _directed_pass(script)

    apply_pass(script, pass_, instruction_engines={"breeze", "indextts2"})
    assert script.utterances[3].render.direction_source == "director"


def test_apply_is_idempotent() -> None:
    script = _script(_ROWS)
    pass_ = _directed_pass(script)

    first = apply_pass(script, pass_)
    snapshot = [
        (u.id, u.render.instruction, u.render.spoken_text, u.render.direction_source)
        for u in script.utterances
    ]
    second = apply_pass(script, pass_)

    assert first and second == []
    assert [
        (u.id, u.render.instruction, u.render.spoken_text, u.render.direction_source)
        for u in script.utterances
    ] == snapshot


def test_an_edited_scene_lands_as_manual_so_a_later_pass_respects_it() -> None:
    script = _script(_ROWS)
    pass_ = _directed_pass(script, decision="edited")
    for scene in pass_.scenes:
        if scene.decision == "edited":
            scene.final_instruction = "My own note."

    apply_pass(script, pass_)
    edited = [u for u in script.utterances if u.render.direction_source == "manual"]
    assert edited and all(u.render.instruction == "My own note." for u in edited)


def test_kept_text_applies_the_note_without_the_tags() -> None:
    """The fix for a rejected scene: keep the direction, keep the author's words."""
    script = _script(_ROWS)
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)
    scene = next(s for s in pass_.scenes if s.tags_status == "kept")
    scene.decision = "kept_text"

    apply_pass(script, pass_)
    lines = [u for u in script.utterances if u.id in scene.line_spoken_text]
    assert all(u.render.instruction for u in lines)
    assert all(u.render.spoken_text is None for u in lines)  # no tags reached the engine
    assert all(u.render.direction_source == "director" for u in lines)


def test_spoken_text_is_only_stored_when_it_differs_from_the_text() -> None:
    """Otherwise an untagged line gets a different cache key for identical audio."""
    script = _script(_ROWS)
    pass_ = _directed_pass(script)
    apply_pass(script, pass_)

    for u in script.utterances:
        if u.render.spoken_text is not None:
            assert u.render.spoken_text != u.text
            assert any(tag in u.render.spoken_text for tag in _TAGS)


def test_apply_survives_a_scene_that_no_longer_exists_in_the_script() -> None:
    script = _script(_ROWS)
    pass_ = _directed_pass(script)
    pass_.scenes.append(
        PassScene(scene_id="c9_s9", status="kept", decision="accepted", instruction="Ghost.")
    )
    apply_pass(script, pass_)  # must not raise


# ---- retry ------------------------------------------------------------------

def test_retry_increments_attempts_and_resets_the_decision() -> None:
    script = _script(_ROWS)
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)
    target = pass_.scenes[0]
    target.decision = "rejected"
    assert target.attempts == 1

    retry_scenes(pass_, script, [target.scene_id], _stub_annotate(instruction="A second take."))

    fresh = pass_.scene(target.scene_id)
    assert fresh.attempts == 2
    assert fresh.decision == "pending"  # the old decision must not survive a new take
    assert fresh.instruction == "A second take."
    assert fresh.status == "kept"


def test_retry_leaves_the_other_scenes_untouched() -> None:
    script = _script(_ROWS)
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)
    before = pass_.scenes[1].model_copy(deep=True)

    retry_scenes(pass_, script, ["c0_s0"], _stub_annotate(instruction="Take two."))

    assert pass_.scenes[1].attempts == before.attempts
    assert pass_.scenes[1].instruction == before.instruction
    assert pass_.counts.scenes == 3


def test_retry_invalidates_the_cache_so_a_later_pass_cannot_resurrect_the_old_take() -> None:
    script = _script(_ROWS)
    cache: dict = {}
    pass_ = run_pass(
        script,
        annotate=fake_annotate(script),
        target_spec=_SPEC,
        model="m",
        prompt_version="pv1",
        cache=cache,
    )
    assert len(cache) == 3

    retry_scenes(
        pass_,
        script,
        ["c0_s0"],
        _stub_annotate(instruction="Take two."),
        target_spec=_SPEC,
        prompt_version="pv1",
        cache=cache,
    )
    assert len(cache) == 2  # the retried scene's reply is gone


# ---- scene note vs. line note, and pace ----

def _pace_annotate(pace, *, scene_note="Elara and Marcus, both avoiding it.", **entry):
    """An annotate that returns a scene-level pace and note."""
    base = {
        "engine_id": "breeze",
        "instruction": "Low and even, no lift at the end.",
        "text": None,
        "rejected_text": None,
        "text_status": "none",
        "text_reason": None,
    }
    base.update(entry)

    def annotate(text, specs):
        return {
            "summary": "s",
            "emotion": "sad",
            "scalar": 0.7,
            "scene_note": scene_note,
            "pace": pace,
            "per_engine": [base],
        }

    return annotate


def _decided(script, annotate, spec=_SPEC, decision="accepted") -> Pass:
    pass_ = run_pass(script, annotate=annotate, target_spec=spec)
    for scene in pass_.scenes:
        if scene.status == "kept":
            scene.decision = decision
    return pass_


def test_a_scene_carries_the_note_and_the_scene_note_separately() -> None:
    scene = direct_scene(_passage(_script(_ROWS)), _pace_annotate(0.9), _SPEC)
    assert scene.instruction == "Low and even, no lift at the end."
    assert scene.scene_note == "Elara and Marcus, both avoiding it."
    assert scene.pace == 0.9


def test_the_pace_is_clamped_and_garbage_becomes_none() -> None:
    passage = _passage(_script(_ROWS))
    assert direct_scene(passage, _pace_annotate(1.9), _SPEC).pace == 1.2
    assert direct_scene(passage, _pace_annotate(0.1), _SPEC).pace == 0.8
    assert direct_scene(passage, _pace_annotate("quickly"), _SPEC).pace is None
    assert direct_scene(passage, _pace_annotate(None), _SPEC).pace is None


def test_the_fake_director_emits_a_scene_note_and_a_varying_pace() -> None:
    script = _script([("c0_p0_s0", "Alpha here.", 0, 0), ("c1_p0_s0", "Is it over?", 1, 0)])
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)
    paces = {s.scene_id: s.pace for s in pass_.scenes}
    assert paces["c0_s0"] == 1.0
    assert paces["c1_s0"] == 0.92  # a question in it: tense, so slightly tighter
    assert all(s.scene_note for s in pass_.scenes)


def test_the_fake_line_note_never_names_the_other_speakers() -> None:
    """F13: the scene note is the right unit, but it is not a line instruction —
    an engine voicing four words of Elara must not be told about Marcus."""
    script = _script(_ROWS)
    for utterance, speaker in zip(script.utterances, ("Elara", "Marcus")):
        utterance.speaker = speaker
        utterance.kind = UtteranceKind.dialogue
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=_SPEC)
    scene = pass_.scene("c0_s0")
    assert "Elara" not in (scene.instruction or "")
    assert "Marcus" not in (scene.instruction or "")
    assert "Elara" in scene.scene_note and "Marcus" in scene.scene_note


def test_apply_writes_intent_on_every_line_whatever_the_engine() -> None:
    """`intent` was a constant — 100% neutral/0.5 in every book, including one
    that had been through a complete director pass."""
    script = _script(_ROWS, engine="kokoro")
    apply_pass(script, _decided(script, _pace_annotate(0.9)))

    directed = [u for u in script.utterances if u.chapter_index == 0]
    assert all(u.intent.emotion == "sad" for u in directed)
    assert all(u.intent.intensity == 0.7 for u in directed)
    assert all(u.intent.pace == 0.9 for u in directed)


def test_a_pace_becomes_render_speed_on_an_engine_that_takes_no_note() -> None:
    """The fix for "a director pass on a Kokoro book directs zero lines"."""
    script = _script(_ROWS, engine="kokoro")
    script.profile = get_profile("general_fiction")  # base_speed 1.0
    apply_pass(script, _decided(script, _pace_annotate(0.9)))

    kokoro = [u for u in script.utterances if u.chapter_index == 0]
    assert all(u.render.speed == 0.9 for u in kokoro)
    assert all(u.render.instruction is None for u in kokoro)  # still no inert note
    assert all(u.render.direction_source == "none" for u in kokoro)


def test_pace_multiplies_the_profiles_base_speed() -> None:
    script = _script(_ROWS, engine="kokoro")
    script.profile = get_profile("literary")  # base_speed 0.96
    apply_pass(script, _decided(script, _pace_annotate(1.2)))
    assert script.utterances[0].render.speed == round(0.96 * 1.2, 3)


def test_pace_is_clamped_to_what_the_renderer_accepts() -> None:
    script = _script(_ROWS, engine="kokoro")
    script.profile = get_profile("general_fiction")
    script.profile.base_speed = 1.25
    apply_pass(script, _decided(script, _pace_annotate(1.2)))
    assert script.utterances[0].render.speed == 1.3  # 1.5 clamped to the ceiling


def test_re_applying_a_pace_never_compounds() -> None:
    """Store the pace, derive from the base — otherwise 0.9 becomes 0.81 becomes
    0.729 and the book slows down every time somebody hits Apply."""
    script = _script(_ROWS, engine="kokoro")
    script.profile = get_profile("general_fiction")
    pass_ = _decided(script, _pace_annotate(0.9))

    first = apply_pass(script, pass_)
    second = apply_pass(script, pass_)
    assert first and second == []
    assert all(u.render.speed == 0.9 for u in script.utterances if u.chapter_index == 0)


def test_a_pace_on_an_instruction_capable_engine_does_not_touch_the_speed() -> None:
    """Breeze gets the note; re-timing it with a speed multiplier as well would
    apply the same direction twice."""
    script = _script(_ROWS, engine="breeze")
    script.profile = get_profile("general_fiction")
    apply_pass(script, _decided(script, _pace_annotate(0.9)))

    line = script.utterances[0]
    assert line.render.speed == 1.0
    assert line.render.instruction == "Low and even, no lift at the end."
    assert line.intent.pace == 0.9  # but the intent still records what was asked


def test_the_scene_note_never_reaches_a_line() -> None:
    script = _script(_ROWS)
    apply_pass(script, _decided(script, _pace_annotate(0.9)))
    stamped = " ".join(u.render.instruction or "" for u in script.utterances)
    assert "Elara and Marcus" not in stamped


def test_a_manual_line_keeps_its_speed_and_its_intent() -> None:
    script = _script(_ROWS, engine="kokoro")
    script.profile = get_profile("general_fiction")
    manual = script.utterances[0]
    manual.render.direction_source = "manual"
    manual.render.speed = 1.15

    apply_pass(script, _decided(script, _pace_annotate(0.9)))
    assert manual.render.speed == 1.15
    assert manual.intent.pace == 1.0


def test_instruction_is_inert_reports_the_engines_that_discard_a_note() -> None:
    assert instruction_is_inert("kokoro", {"breeze"}) is True
    assert instruction_is_inert("breeze", {"breeze", "indextts2"}) is False


# ---- what the director is shown, and what it is allowed to touch ------------
# Every case below was observed on the wire, in requests recorded against a
# stub gateway, not reasoned about.


def _script_with_heading(engine: str = "kokoro") -> BookScript:
    script = _script(
        [
            ("c0_head", "Chapter One: The Ledger", 0, 0),
            ("c0_p0_s0", "It was 1943.", 0, 0),
            ("c0_p1_s0", "Is it over?", 0, 0),
        ],
        engine=engine,
    )
    script.utterances[0].kind = UtteranceKind.heading
    return script


def test_a_chapter_announcement_is_not_part_of_the_scene_the_director_reads() -> None:
    """The title was handed to the model as the scene's opening prose, so a tag
    could be placed on it and `distribute_tags` would attach the tag to the
    announcement."""
    passage = _passage(_script_with_heading())
    assert "Chapter One" not in passage.text
    assert passage.text.startswith("It was 1943.")
    assert passage.directed_line_ids == ["c0_p0_s0", "c0_p1_s0"]


def test_a_scene_still_covers_every_line_including_the_announcement() -> None:
    """The studio's scene rail is built from ``line_ids`` and the contract says
    the scenes of a chapter cover its lines exactly once — so excluding the
    heading from the *direction* must not exclude it from the *scene*."""
    script = _script_with_heading()
    covered = [lid for p in scene_passages(script) for lid in p.line_ids]
    assert covered == [u.id for u in script.utterances]


def test_a_heading_is_never_stamped_with_the_scenes_direction() -> None:
    """Observed: "Chapter One: The Ledger" was synthesized at speed 0.92 because
    a tense exchange later in the same scene asked for 0.92."""
    script = _script_with_heading()
    script.profile = get_profile("general_fiction")  # base_speed 1.0
    apply_pass(script, _decided(script, _pace_annotate(0.9)))

    heading = script.utterances[0]
    assert heading.render.speed == 1.0
    assert heading.render.instruction is None
    assert heading.render.spoken_text is None
    assert heading.intent.pace == 1.0 and heading.intent.emotion == "neutral"
    # …while the prose of the same scene did take the direction.
    assert script.utterances[1].render.speed == 0.9


def test_a_pass_does_not_direct_a_chapter_the_book_never_reads_aloud() -> None:
    """A copyright page is ``include_in_audio: false``; directing it spends a
    model call (2 of the 7 scenes of hard-manuscript.txt) on audio never made."""
    script = _script(
        [("c0_p0_s0", "Copyright 2026.", 0, 0), ("c1_p0_s0", "Real prose here.", 1, 0)]
    )
    script.chapters[0].include_in_audio = False

    pass_ = run_pass(script, annotate=_stub_annotate(instruction="Low."), target_spec=_SPEC)
    assert pass_.scope.scenes == ["c1_s0"]
    assert [s.scene_id for s in pass_.scenes] == ["c1_s0"]

    # Naming the scene explicitly still directs it — the filter is a default,
    # not a prohibition.
    named = run_pass(
        script, scenes=["c0_s0"], annotate=_stub_annotate(instruction="Low."), target_spec=_SPEC
    )
    assert [s.scene_id for s in named.scenes] == ["c0_s0"]


def test_the_cli_resolves_a_target_engine_from_the_book_it_was_given() -> None:
    """F2's shape: a pass written for Breeze on a book with no Breeze line in it
    stamps nothing. The CLI hardcoded ``breeze``; the studio already resolved it."""
    from pipeline.direct import default_target_engine

    capable = ["breeze", "indextts2"]
    mixed = _script(_ROWS, engine="kokoro")
    mixed.utterances[0].render.engine = "breeze"
    mixed.utterances[1].render.engine = "breeze"
    mixed.utterances[2].render.engine = "indextts2"
    assert default_target_engine(mixed, capable) == "breeze"
    assert default_target_engine(_script(_ROWS, engine="kokoro"), capable) == ""
