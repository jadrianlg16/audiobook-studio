"""scripts/direction_ab.py — the blind A/B test of the director's notes.

The listening itself is a person's job; what can be checked offline is that the
test is fair and scored right: the three takes of a passage differ in the note
alone, loudness is matched so "louder" cannot win, and unblinding maps each
answer back to the right condition.
"""

from __future__ import annotations

import array
import importlib.util
import json
import math
import sys
import wave
from pathlib import Path

import pytest

from pipeline.build import build_book_script
from pipeline.direct import apply_pass, fake_annotate, run_pass
from pipeline.direct import DEFAULT_SPECS

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "direction_ab.py"


@pytest.fixture(scope="module")
def ab():
    spec = importlib.util.spec_from_file_location("direction_ab", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["direction_ab"] = module
    spec.loader.exec_module(module)
    return module


def _directed(tmp_path: Path):
    text = (
        "Rain hammered the windows as Elara climbed the last of the stairs.\n\n"
        "“You let it go out,” she said.\n\n"
        "Marcus did not look up. “The oil ran dry an hour ago,” he answered.\n"
    )
    src = tmp_path / "p.txt"
    src.write_text(text, encoding="utf-8")
    script = build_book_script(str(src), engine="breeze", book_type="fiction", multi_voice=False)
    pass_ = run_pass(script, annotate=fake_annotate(script), target_spec=dict(DEFAULT_SPECS["breeze"]))
    for scene in pass_.scenes:
        if scene.status == "kept":
            scene.decision = "accepted"
        elif scene.status == "rejected" and scene.instruction:
            scene.decision = "kept_text"
    apply_pass(script, pass_, instruction_engines={"breeze"})
    return script, json.loads(pass_.model_dump_json())


def test_the_three_takes_differ_only_in_the_note(ab, tmp_path):
    directed, pass_ = _directed(tmp_path)
    assert any(u.render.instruction for u in directed.utterances), "fake director stamped nothing"
    conds = ab._condition_scripts(directed, pass_)

    emotions = {s["emotion"] for s in pass_["scenes"]}
    for i, u in enumerate(directed.utterances):
        a, b, c = (conds[k].utterances[i] for k in "ABC")
        assert a.text == b.text == c.text
        for field in ("engine", "voice_id", "spoken_text", "voice_design", "lead_pause_ms", "trail_pause_ms"):
            assert getattr(a.render, field) == getattr(b.render, field) == getattr(c.render, field), field
        # pace is pinned so a speed change cannot pass for the note's effect
        assert a.render.speed == b.render.speed == c.render.speed == 1.0
        assert a.render.instruction is None
        assert b.render.instruction == u.render.instruction
        if u.render.instruction:
            assert c.render.instruction in emotions | {"neutral"}
            assert len(c.render.instruction.split()) <= 2
        else:
            assert c.render.instruction is None
    # the directed script itself is not mutated
    assert any(u.render.instruction for u in directed.utterances)


def _wav(path: Path, amplitude: float, seconds: float = 0.5, rate: int = 24000) -> None:
    n = int(seconds * rate)
    pcm = array.array("h", (int(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(n)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _rms_dbfs(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        pcm = array.array("h", w.readframes(w.getnframes()))
    return 20 * math.log10(math.sqrt(sum(s * s for s in pcm) / len(pcm)) / 32768)


def test_quiet_and_loud_takes_come_out_equally_loud(ab, tmp_path):
    _wav(tmp_path / "quiet.wav", 0.05)
    _wav(tmp_path / "loud.wav", 0.6)
    q = ab._loudness_match(tmp_path / "quiet.wav", tmp_path / "q.wav")
    loud = ab._loudness_match(tmp_path / "loud.wav", tmp_path / "l.wav")
    assert abs(_rms_dbfs(tmp_path / "q.wav") - _rms_dbfs(tmp_path / "l.wav")) < 0.2
    assert abs(_rms_dbfs(tmp_path / "q.wav") - ab.TARGET_RMS_DBFS) < 0.2
    assert q["seconds"] == loud["seconds"] == 0.5
    assert not q["peak_capped"] and not loud["peak_capped"]


def test_a_take_that_would_clip_is_capped_not_clipped(ab, tmp_path):
    # one loud click in near-silence: reaching -20 dBFS RMS would clip the click
    rate = 24000
    pcm = array.array("h", [0] * rate)
    pcm[100] = 30000
    with wave.open(str(tmp_path / "click.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    info = ab._loudness_match(tmp_path / "click.wav", tmp_path / "out.wav")
    with wave.open(str(tmp_path / "out.wav"), "rb") as w:
        out = array.array("h", w.readframes(w.getnframes()))
    assert max(abs(s) for s in out) <= int(ab.PEAK_CAP * 32768) + 1
    assert info["peak_capped"]


def test_scoring_unblinds_each_answer_to_its_condition(ab, tmp_path, capsys):
    work = tmp_path / "work"
    (work / "listen").mkdir(parents=True)
    key = {"trials": {}}
    answers = {}
    # 8 part-1 passages: B is take 2 in half of them, take 1 in the rest; the
    # listener keeps B six times -> 75% -> PASS.
    for i in range(8):
        order = ["A", "B"] if i % 2 else ["B", "A"]
        key["trials"][f"t{i}"] = {"passage": f"p{i}", "part": 1, "takes": order}
        b_index = order.index("B")
        keep = b_index if i < 6 else 1 - b_index
        answers[f"t{i}"] = {"keep": keep, "ratings": [3, 4], "follows": b_index if i < 5 else "cant_tell"}
    key["trials"]["u0"] = {"passage": "p0", "part": 2, "takes": ["C", "B"]}
    answers["u0"] = {"keep": 0, "ratings": [2, 5], "follows": None}
    (work / "key.json").write_text(json.dumps(key), encoding="utf-8")
    (work / "listen" / "results.json").write_text(json.dumps({"answers": answers}), encoding="utf-8")

    assert ab.main(["--work", str(work), "score"]) == 0
    score = json.loads((work / "score.json").read_text(encoding="utf-8"))
    part1 = score["1"]
    assert part1["wins"] == {"A": 2, "B": 6}
    assert part1["verdict"] == "PASS"
    assert part1["note_audible"] == {"right": 5, "wrong": 0, "cant_tell": 3}
    assert score["2"]["wins"] == {"B": 0, "C": 1}
    assert score["2"]["mean_rating"] == {"B": 5.0, "C": 2.0}

    # one fewer B win is below the threshold
    answers["t5"]["keep"] = 1 - key["trials"]["t5"]["takes"].index("B")
    (work / "listen" / "results.json").write_text(json.dumps({"answers": answers}), encoding="utf-8")
    ab.main(["--work", str(work), "score"])
    assert json.loads((work / "score.json").read_text(encoding="utf-8"))["1"]["verdict"] == "FAIL"


def test_unanswered_trials_are_left_out_not_counted_as_losses(ab, tmp_path):
    work = tmp_path / "work"
    (work / "listen").mkdir(parents=True)
    key = {"trials": {"t0": {"passage": "p0", "part": 1, "takes": ["A", "B"]},
                      "t1": {"passage": "p1", "part": 1, "takes": ["B", "A"]}}}
    answers = {"t0": {"keep": 1, "ratings": [2, 4], "follows": 1}, "t1": {"keep": None, "ratings": [None, None]}}
    (work / "key.json").write_text(json.dumps(key), encoding="utf-8")
    (work / "listen" / "results.json").write_text(json.dumps({"answers": answers}), encoding="utf-8")
    ab.main(["--work", str(work), "score"])
    part1 = json.loads((work / "score.json").read_text(encoding="utf-8"))["1"]
    assert part1["n"] == 1 and part1["wins"] == {"A": 0, "B": 1}
