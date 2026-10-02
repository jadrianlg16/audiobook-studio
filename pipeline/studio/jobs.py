"""Jobs: one worker thread, a FIFO queue, and a job record that is a **file**.

Three decisions worth the words:

**A job record is a file, not memory.** ``output/<slug>/jobs/<id>.json`` is
written atomically after every progress step, and the in-memory index is rebuilt
from those files at startup. That is what makes semantic #5 of the contract true
("jobs survive restarts"): a `running` job found on disk at boot never was
finished, so it becomes ``interrupted`` and ``POST /resume`` re-queues it. The
render cache means resuming a book that was 90% rendered costs the last 10%.

**Pause and cancel are cooperative, never a kill.** Both set a flag that the
renderer (between lines) and the director (between scenes) poll. Killing a
thread mid-synth would leave a half-written cache entry that hashes as a valid
take — silently wrong audio, which is the failure mode this whole codebase is
built to avoid.

**One job at a time.** A single worker thread, because the thing on the other
end is one GPU or one Ollama process; running two renders concurrently makes
both slower and the progress numbers meaningless.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable

from ..direct import (
    FAKE_MODEL,
    FAKE_PROMPT_VERSION,
    DEFAULT_SPECS,
    PassCounts,
    Pass,
    apply_pass,  # noqa: F401  (re-exported for api.py's convenience)
    fake_annotate,
    http_annotate,
    new_pass_id,
    resolve_spec,
    retry_scenes,
    run_pass,
    scene_passages,
)
from ..master import MasterError, master_book
from ..render import RenderStopped, fake_synth, http_synth
from . import store
from .models import Job, JobChapter, JobLogEntry, JobProgress, JobSummary

_LOG_LIMIT = 200

# The spec used when the target engine's real one cannot be fetched. Breeze is
# the default expressive engine (STUDIO_EXPRESSIVE_ENGINE), so its shape is the
# useful fallback — but the job log always says it was used, because a tag list
# that has drifted from the engine's own config is a real way to get rejections
# nobody can explain.
_FALLBACK_SPEC: dict[str, Any] = dict(DEFAULT_SPECS["breeze"])


def _now() -> str:
    return store.now()


class JobManager:
    """Owns the queue, the worker thread, the job index and the SSE fan-out."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._flags: dict[str, dict[str, bool]] = {}
        self._subs: dict[str, list[queue.Queue]] = {}
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._running: str | None = None
        self._started_wall: dict[str, float] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.rebuild()
        store.register_job_provider(self.last_job_for)
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._shutdown = False
                self._worker = threading.Thread(
                    target=self._loop, name="studio-jobs", daemon=True
                )
                self._worker.start()

    def stop(self) -> None:
        self._shutdown = True
        self._wake.set()

    def rebuild(self) -> None:
        """Re-read every job file. Anything left ``running``/``queued`` by a
        previous process is ``interrupted`` — this process never started it."""
        with self._lock:
            self._jobs.clear()
            self._queue.clear()
            self._running = None
            for slug in store.list_slugs():
                directory = os.path.join(store.book_dir(slug), "jobs")
                if not os.path.isdir(directory):
                    continue
                for name in sorted(os.listdir(directory)):
                    if not name.endswith(".json"):
                        continue
                    raw = store._read_json(os.path.join(directory, name))
                    if not isinstance(raw, dict):
                        continue
                    try:
                        job = Job.model_validate(raw)
                    except Exception:  # noqa: BLE001 - a corrupt record must not stop boot
                        continue
                    if job.status in ("running", "queued"):
                        job.status = "interrupted"
                        job.error = job.error or "the studio restarted while this job was running"
                        self._write(job)
                    self._jobs[job.id] = job

    # -- persistence + events ------------------------------------------------

    def _write(self, job: Job) -> None:
        """Persist a job record. Serialized, and never fatal.

        Serialized because the worker thread and whatever HTTP thread just
        paused the job both write the same file. Never fatal because this runs
        inside the render loop: a transient filesystem error (a virus scanner
        holding the file open for a moment is the classic one) must cost a
        progress update, not the whole job — and not the worker thread, which
        would silently hang every job queued behind it.
        """
        book_dir = store.book_dir(job.book)
        if not os.path.isdir(book_dir):
            # The book was deleted while this job was in flight. Writing the
            # record would re-create `output/<slug>/jobs/` under a slug the API
            # 404s everywhere else — an invisible orphan folder that only a
            # second DELETE cleans up.
            return
        path = os.path.join(book_dir, "jobs", f"{job.id}.json")
        try:
            with self._io_lock:
                store._write_json(path, job.model_dump_json(indent=2))
        except OSError:
            pass

    def _emit(self, job: Job, event: str, data: Any) -> None:
        with self._lock:
            subscribers = list(self._subs.get(job.id, ()))
        payload = json.dumps(data, ensure_ascii=False, default=str)
        for q in subscribers:
            try:
                q.put_nowait((event, payload))
            except queue.Full:  # a subscriber that stopped draining is dropped
                pass

    def _progress_payload(self, job: Job) -> dict:
        data = job.model_dump()
        data.pop("log", None)
        return data

    def touch(self, job: Job) -> None:
        job.progress.elapsed_s = round(self._elapsed(job), 2)
        self._write(job)
        self._emit(job, "progress", self._progress_payload(job))

    def log(self, job: Job, line: str) -> None:
        entry = JobLogEntry(ts=_now(), line=line)
        job.log.append(entry)
        if len(job.log) > _LOG_LIMIT:
            del job.log[: len(job.log) - _LOG_LIMIT]
        self._write(job)
        self._emit(job, "log", entry.model_dump())

    def _elapsed(self, job: Job) -> float:
        """Wall time of the *current* run. A resumed job restarts the clock —
        reporting "4 h elapsed" for a job that ran for four minutes after an
        overnight interruption would make the ETA nonsense."""
        started = self._started_wall.get(job.id)
        if started is None:
            return job.progress.elapsed_s
        return time.time() - started

    # -- subscriptions -------------------------------------------------------

    def subscribe(self, job_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: queue.Queue) -> None:
        with self._lock:
            listeners = self._subs.get(job_id)
            if listeners and q in listeners:
                listeners.remove(q)
            if listeners is not None and not listeners:
                self._subs.pop(job_id, None)

    # -- queries -------------------------------------------------------------

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise store.NotFound(f"no such job: {job_id!r}")
        return job

    def list(self) -> list[JobSummary]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [j.summary() for j in jobs]

    def last_job_for(self, slug: str) -> JobSummary | None:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.book == slug]
        if not jobs:
            return None
        active = [j for j in jobs if j.status in ("running", "queued", "paused")]
        pool = active or jobs
        pool.sort(key=lambda j: j.created_at, reverse=True)
        return pool[0].summary()

    def active_for(self, slug: str, kind: str | None = None) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.book != slug or job.status not in ("running", "queued"):
                    continue
                if kind is None or job.kind == kind:
                    return job
        return None

    # -- submission ----------------------------------------------------------

    def submit(self, kind: str, slug: str, request: dict) -> Job:
        job = Job(
            id=f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            kind=kind,  # type: ignore[arg-type]
            book=slug,
            status="queued",
            created_at=_now(),
            request=request,
            progress=JobProgress(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._flags[job.id] = {"pause": False, "cancel": False}
            self._queue.append(job.id)
        self._write(job)
        self._wake.set()
        return job

    # -- controls ------------------------------------------------------------

    def pause(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._lock:
            self._flags.setdefault(job_id, {})["pause"] = True
            if job.status == "queued":
                if job_id in self._queue:
                    self._queue.remove(job_id)
                job.status = "paused"
        self.touch(job)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._lock:
            self._flags.setdefault(job_id, {})["cancel"] = True
            if job.status in ("queued", "paused"):
                if job_id in self._queue:
                    self._queue.remove(job_id)
                job.status = "cancelled"
                job.finished_at = _now()
        self.touch(job)
        return job

    def resume(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status not in ("paused", "interrupted", "failed"):
            return job
        with self._lock:
            self._flags[job_id] = {"pause": False, "cancel": False}
            job.status = "queued"
            job.error = None
            job.finished_at = None
            if job_id not in self._queue:
                self._queue.append(job_id)
        self.touch(job)
        self._wake.set()
        return job

    def cancel_book_jobs(self, slug: str, timeout: float = 10.0) -> list[str]:
        """Cancel every queued/running job for ``slug`` and wait for the worker.

        Deleting a book out from under a running render used to be a silent
        race: the DELETE removed the folder, the render died a second later, and
        its failure handler re-created ``output/<slug>/jobs/``. Cancelling first is the honest order of operations — cancellation is
        cooperative, so this waits (bounded) for the worker to actually put the
        job down rather than assuming it did.

        Returns the ids it cancelled. The wait is bounded because a synth call
        already in flight can outlive any timeout we would be willing to make a
        person sit through; ``_write`` skipping a deleted book covers that case.
        """
        with self._lock:
            ids = [
                job.id for job in self._jobs.values()
                if job.book == slug and job.status in ("running", "queued", "paused")
            ]
        for job_id in ids:
            try:
                self.cancel(job_id)
            except store.NotFound:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.active_for(slug) is None and self._running not in ids:
                break
            time.sleep(0.05)
        return ids

    def forget_book(self, slug: str) -> list[str]:
        """Drop a deleted book's jobs from the in-memory index.

        Their files went with the folder, so they would vanish on the next
        restart anyway; until then they were ghosts in ``GET /jobs`` pointing at
        a book that 404s everywhere else."""
        with self._lock:
            ids = [job_id for job_id, job in self._jobs.items() if job.book == slug]
            for job_id in ids:
                self._jobs.pop(job_id, None)
                self._flags.pop(job_id, None)
                self._started_wall.pop(job_id, None)
                if job_id in self._queue:
                    self._queue.remove(job_id)
        return ids

    def _stopped(self, job_id: str) -> bool:
        flags = self._flags.get(job_id) or {}
        return bool(flags.get("pause") or flags.get("cancel"))

    # -- the worker ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._shutdown:
            job_id = None
            with self._lock:
                while self._queue:
                    candidate = self._queue.popleft()
                    job = self._jobs.get(candidate)
                    if job is not None and job.status == "queued":
                        job_id = candidate
                        break
            if job_id is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                self._run(job_id)
            except BaseException as exc:  # noqa: BLE001 - the worker must outlive any job
                # `_run` already turns a failing handler into a `failed` job, so
                # reaching here means the bookkeeping itself broke. Letting it
                # escape kills the only worker thread, and every later job then
                # sits at "queued" forever with nothing in any log to say why.
                job = self._jobs.get(job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = f"job runner crashed: {type(exc).__name__}: {exc}"
                    job.finished_at = _now()
                    self._write(job)

    def _run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job.status = "running"
        job.started_at = job.started_at or _now()
        job.error = None
        self._started_wall[job.id] = time.time()
        self._running = job.id
        self.touch(job)
        outcome: str = "done"
        error: str | None = None
        try:
            handler = {
                "render": self._run_render,
                "direct": self._run_direct,
                "export": self._run_export,
            }[job.kind]
            handler(job)
        except RenderStopped as exc:
            outcome = "cancelled" if (self._flags.get(job.id) or {}).get("cancel") else "paused"
            self.log(job, f"stopped: {exc}")
        except Exception as exc:  # noqa: BLE001 - a failed job is data, not a crash
            outcome = "failed"
            error = f"{type(exc).__name__}: {exc}"
            self.log(job, f"failed: {error}")
            self.log(job, traceback.format_exc().strip().splitlines()[-1])
        finally:
            job.error = error
            job.progress.elapsed_s = round(
                time.time() - self._started_wall.get(job.id, time.time()), 2
            )
            # The record is a live object that HTTP readers poll while this
            # thread mutates it, so `finished_at` is written *before* the
            # terminal status: a caller that sees "cancelled" has necessarily
            # already seen the timestamp that goes with it. The other order
            # leaks a cancelled job with `finished_at: null` for a few
            # microseconds — rare, real, and exactly the kind of flake nobody
            # can reproduce on purpose.
            job.finished_at = _now() if outcome in ("done", "failed", "cancelled") else None
            job.status = outcome  # type: ignore[assignment]
            self._running = None
            self._write(job)
            self._emit(job, "progress", self._progress_payload(job))
            self._emit(job, "done", job.model_dump())

    # -- render --------------------------------------------------------------

    def _run_render(self, job: Job) -> None:
        slug = job.book
        request = job.request or {}
        scope = request.get("scope") or "stale"

        derived = store.derive(slug)
        by_index = {c.index: c for c in derived.chapters()}
        if scope == "all":
            selected = sorted(by_index)
        elif scope == "stale":
            # "Not fresh", not "has a non-cached line". A single-line re-render
            # leaves every line cached and the chapter WAV stitched from the
            # take that line replaced — the state that made Export permanently
            # unavailable with nothing on screen admitting why.
            # This scope is the button that gets you out of it.
            selected = sorted(i for i, c in by_index.items() if not c.fresh)
        elif scope == "chapters":
            selected = sorted(set(request.get("chapters") or []) & set(by_index))
        elif scope == "lines":
            wanted = set(request.get("line_ids") or [])
            selected = sorted({
                line.chapter_index for line in derived.lines if line.id in wanted
            })
        else:
            raise ValueError(f"unknown render scope {scope!r}")

        included = derived.included()
        excluded = [i for i in selected if i not in included]
        if excluded:
            selected = [i for i in selected if i in included]
            self.log(
                job,
                f"skipping chapter(s) {', '.join(str(i) for i in excluded)}: "
                f"marked include_in_audio=false",
            )

        script = store.load_script(slug)
        _graft_previous_timings(slug, script)

        chapter_jobs: dict[int, JobChapter] = {}
        for chapter in script.chapters:
            total = sum(1 for u in script.utterances if u.chapter_index == chapter.index)
            chapter_jobs[chapter.index] = JobChapter(
                index=chapter.index,
                title=chapter.title,
                total=total,
                status="queued" if chapter.index in selected else "skipped",
            )
        job.chapters = [chapter_jobs[i] for i in sorted(chapter_jobs)]
        # A resumed render walks its whole scope again (the cache makes that
        # nearly free), so the counters restart with it — carrying them over
        # would report 31 of 19 lines done.
        job.progress.done = 0
        job.progress.cache_hits = 0
        job.progress.synth_calls = 0
        job.progress.total = sum(chapter_jobs[i].total for i in selected)
        self.log(
            job,
            f"render {scope}: {len(selected)} of {len(chapter_jobs)} chapters, "
            f"{job.progress.total} lines"
            + ("  [offline: fake synth]" if store.offline() else ""),
        )
        self.touch(job)

        if not selected:
            job.result = {"chapters_written": [], "duration_ms": sum(c.duration_ms for c in script.chapters)}
            self.log(job, "nothing to render — every chapter in scope is already fresh")
            return

        # Contract rule 2's sibling for the *written* note: a manual instruction
        # typed into the Inspector on a Kokoro line used to travel to the engine,
        # which cannot take one. The catalog is merged (gateway + static), so the
        # gate still holds with the gateway down.
        synth = (
            fake_synth()
            if store.offline()
            else http_synth(
                store.gateway_url(), instruction_engines=store.instruction_engines()
            )
        )
        renderer = store.make_renderer(slug, synth=synth)
        counters = {"synth_seconds": 0.0, "audio_ms": 0}
        started = time.time()

        def on_line(utterance, key: str, cached: bool, seconds: float) -> None:
            entry = chapter_jobs[utterance.chapter_index]
            if entry.status == "queued":
                entry.status = "running"
            entry.done += 1
            entry.seconds = round(entry.seconds + seconds, 3)
            engine = utterance.render.engine
            entry.engines[engine] = entry.engines.get(engine, 0) + 1
            job.progress.done += 1
            if cached:
                job.progress.cache_hits += 1
            else:
                job.progress.synth_calls += 1
                counters["synth_seconds"] += seconds
                counters["audio_ms"] += store._wav_duration_ms(renderer.cache_path(key)) or 0
            # RTF over the lines actually synthesized: a cache hit takes no wall
            # time, so folding its audio in would report an engine speed nobody
            # can reproduce.
            if counters["audio_ms"] > 0:
                job.progress.rtf = round(counters["synth_seconds"] / (counters["audio_ms"] / 1000), 3)
            elapsed = time.time() - started
            if job.progress.done and job.progress.total:
                per = elapsed / job.progress.done
                job.progress.eta_s = round(per * (job.progress.total - job.progress.done), 1)
            self.log(
                job,
                f"{utterance.id}  {'cache' if cached else 'synth'}  {engine}  "
                f"{seconds:.2f}s → {key[:4]}…{key[-3:]}",
            )
            if entry.done >= entry.total:
                entry.status = "done"
            self.touch(job)

        renderer.render(
            script,
            store.book_dir(slug),
            chapters=selected,
            on_line=on_line,
            should_stop=lambda: self._stopped(job.id),
        )

        store._write_json(store.manifest_path(slug), script.model_dump_json(indent=2))
        store.copy_player(slug)
        # Which synth answered is a property of the audio, not of today's flag:
        # stamped here so `BookSummary.synthetic` still says "test audio" after
        # STUDIO_OFFLINE goes back to 0.
        store.mark_rendered(slug, store.render_mode())
        store.reset_chapter_qc(slug, selected)  # a rewritten take is unheard again
        job.result = {
            "chapters_written": selected,
            "duration_ms": sum(c.duration_ms for c in script.chapters),
        }
        self.log(
            job,
            f"wrote manifest.json and {len(selected)} chapter WAV(s); "
            f"{job.progress.cache_hits} cache hits, {job.progress.synth_calls} synth calls",
        )

    # -- direct --------------------------------------------------------------

    def _run_direct(self, job: Job) -> None:
        slug = job.book
        request = job.request or {}
        script = store.load_script(slug)

        # The engine this book actually uses beats the global default: a pass
        # written for an engine no line is routed to stamps nothing and says
        # nothing. `/direct` resolves this too, so the value is
        # normally already in the request — this is the CLI/resume path.
        target_engine = request.get("target_engine") or store.default_target_engine(script)
        spec = self._spec_for(job, target_engine)

        if store.fake_director():
            annotate = fake_annotate(script)
            provider, model, prompt_version = "ollama", FAKE_MODEL, FAKE_PROMPT_VERSION
            self.log(job, "director: fake (STUDIO_FAKE_DIRECTOR=1) — no LLM is called")
        else:
            director = store.resolved_director()
            provider = request.get("provider") or director.provider
            model = request.get("model") or director.model
            prompt_version = store.remote_prompt_version()
            annotate = http_annotate(store.gateway_url(), provider, model)
            self.log(job, f"director: {provider}:{model} prompt {prompt_version or '(unknown)'}")

        if request.get("retry"):
            self._run_retry(job, script, spec, annotate, prompt_version)
            return

        scenes = _direct_scope(script, request)
        pass_id = (job.result or {}).get("pass_id") or new_pass_id()
        # Published before the first scene so a resume finds the pass to
        # continue — and shaped like the finished result from the start, so the
        # review screen never has to guard against a half-formed one.
        job.result = {"pass_id": pass_id, "counts": PassCounts().model_dump()}
        existing: Pass | None = None
        try:
            existing = store.load_pass(slug, pass_id)
        except store.NotFound:
            existing = None
        if existing is not None:
            already = {s.scene_id for s in existing.scenes}
            scenes = [s for s in scenes if s not in already]
            self.log(job, f"resuming pass {pass_id}: {len(already)} scenes already directed")

        job.progress.total = len(scenes) + (len(existing.scenes) if existing else 0)
        job.progress.done = len(existing.scenes) if existing else 0
        job.chapters = _direct_chapter_rows(script, scenes)
        self.log(job, f"directing {len(scenes)} scene(s) for target engine {target_engine}")
        self.touch(job)

        cache = store.load_scene_cache(slug)
        chapter_rows = {row.index: row for row in job.chapters}

        def checkpoint(current: Pass) -> None:
            store.save_pass(slug, _merge(existing, current))
            store.save_scene_cache(slug, cache)

        def on_progress(done: int, total: int, scene) -> None:
            job.progress.done += 1
            row = chapter_rows.get(scene.chapter_index)
            if row is not None:
                row.done += 1
                row.status = "done" if row.done >= row.total else "running"
            self.log(job, f"{scene.scene_id}  {scene.status}  {scene.reason or scene.error or ''}".rstrip())
            self.touch(job)

        fresh = run_pass(
            script,
            scenes=scenes,
            annotate=annotate,
            target_spec=spec,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            pass_id=pass_id,
            book=slug,
            checkpoint=checkpoint,
            should_stop=lambda: self._stopped(job.id),
            on_progress=on_progress,
            cache=cache,
        )
        final = _merge(existing, fresh)
        store.save_pass(slug, final)
        store.save_scene_cache(slug, cache)
        job.result = {"pass_id": final.id, "counts": final.counts.model_dump()}
        self.log(
            job,
            f"pass {final.id}: kept {final.counts.kept}, rejected {final.counts.rejected}, "
            f"ignored {final.counts.ignored}, error {final.counts.error}",
        )
        if self._stopped(job.id):
            raise RenderStopped(f"pass {final.id} stopped after {len(fresh.scenes)} scene(s)")

    def _run_retry(self, job: Job, script, spec, annotate, prompt_version: str) -> None:
        slug = job.book
        request = job.request or {}
        pass_id = request["pass_id"]
        scenes = list(request.get("scenes") or [])
        pass_ = store.load_pass(slug, pass_id)
        job.result = {"pass_id": pass_id, "counts": pass_.counts.model_dump()}
        job.progress.total = len(scenes)
        job.chapters = _direct_chapter_rows(script, scenes)
        chapter_rows = {row.index: row for row in job.chapters}
        self.log(job, f"retrying {len(scenes)} scene(s) in pass {pass_id}")
        self.touch(job)
        cache = store.load_scene_cache(slug)

        def on_progress(done: int, total: int, scene) -> None:
            job.progress.done = done
            row = chapter_rows.get(scene.chapter_index)
            if row is not None:
                row.done += 1
                row.status = "done" if row.done >= row.total else "running"
            self.log(job, f"{scene.scene_id}  retry #{scene.attempts}  {scene.status}")
            self.touch(job)

        updated = retry_scenes(
            pass_,
            script,
            scenes,
            annotate,
            target_spec=spec,
            prompt_version=prompt_version or None,
            cache=cache,
            checkpoint=lambda p: store.save_pass(slug, p),
            on_progress=on_progress,
        )
        store.save_pass(slug, updated)
        store.save_scene_cache(slug, cache)
        job.result = {"pass_id": updated.id, "counts": updated.counts.model_dump()}

    def _spec_for(self, job: Job, engine_id: str) -> dict:
        """The target engine's live spec, or a stated fallback.

        ``resolve_spec`` prefers the gateway because the tag vocabulary is
        engine-side config; when it can't be reached the pass still has to run,
        so a Breeze-shaped spec stands in — and the job log says so, because a
        pass directed against the wrong tag list produces rejections that look
        like a model problem."""
        try:
            spec = resolve_spec(engine_id, None if store.fake_director() else store.gateway_url())
            if spec.get("id") != engine_id:
                spec = dict(spec, id=engine_id)
            return spec
        except Exception as exc:  # noqa: BLE001 - SystemExit included (no built-in spec)
            self.log(
                job,
                f"! could not resolve the spec for {engine_id!r} ({type(exc).__name__}: {exc}); "
                f"using the built-in Breeze-like spec — its audio_tags may not match the engine",
            )
            return dict(_FALLBACK_SPEC, id=engine_id)

    # -- export --------------------------------------------------------------

    def _run_export(self, job: Job) -> None:
        slug = job.book
        request = job.request or {}
        defaults = store.core_settings().export
        fmt = request.get("format") or defaults.format
        loudness = request.get("loudness") or defaults.loudness
        sample_rate = int(request.get("sample_rate") or defaults.sample_rate)
        bitrate = request.get("bitrate") or ("192k" if fmt == "acx" else None)
        record = store.load_book(slug)

        job.progress.total = 100
        self.log(
            job,
            f"mastering {fmt} · loudness {loudness} · {sample_rate} Hz"
            + (f" · {bitrate}" if bitrate else ""),
        )
        if store.render_mode() == "offline" or record.get("render_mode") == "offline":
            self.log(job, "! this book's audio came from the offline fake synth — the export is test audio")
        self.touch(job)

        def on_progress(stage: str, fraction: float) -> None:
            job.progress.done = int(fraction * 100)
            self.log(job, f"{stage}  {fraction * 100:.0f}%")
            self.touch(job)

        kwargs: dict[str, Any] = dict(
            format=fmt,
            loudness=loudness,
            sample_rate=sample_rate,
            title=record.get("title") or slug,
            author=record.get("author") or "",
            on_progress=on_progress,
        )
        if bitrate:
            kwargs["bitrate"] = bitrate
        with self._masterable_manifest(job, slug):
            try:
                result = master_book(store.book_dir(slug), **kwargs)
            except MasterError as exc:
                raise RuntimeError(str(exc)) from exc

        job.progress.done = 100
        files = self._record_export_files(slug, result, loudness, sample_rate, bitrate)
        if len(files) > 1 or getattr(result, "files", None):
            # ACX ships one MP3 per chapter with its own measurement, so the
            # result is a table, not a file. `compliance` is the one-word answer
            # a producer actually acts on.
            job.result = {
                "files": files,
                "compliance": self._compliance(result, files),
                "format": getattr(result, "format", fmt),
            }
            self.log(
                job,
                f"wrote {len(files)} file(s) to exports/ · compliance "
                f"{job.result['compliance']}",
            )
            return

        measured = getattr(result, "measured", None) or {}
        job.result = {
            "file": result.file,
            "url": f"{store.public_prefix()}/books/{slug}/exports/{result.file}",
            "bytes": result.bytes,
            "format": result.format,
            "loudness": result.loudness,
        }
        self.log(
            job,
            f"wrote exports/{result.file} ({result.bytes:,} bytes)"
            + (
                f" · {measured['integrated_lufs']:.1f} LUFS / {measured['true_peak_dbtp']:.1f} dBTP"
                if measured else ""
            ),
        )

    @contextmanager
    def _masterable_manifest(self, job: Job, slug: str):
        """Hide ``include_in_audio: false`` chapters from the mastering step.

        ``master_book`` reads ``manifest.json`` from the book folder and refuses
        a chapter with no audio file — which is exactly what an excluded chapter
        is, since the render skipped it. Rather than teach the exporter about a
        studio-level flag (``master.py`` stays a pure book-folder tool, and the
        flag may yet move onto the schema), the manifest is narrowed for the duration of
        the run and restored in ``finally``: the player keeps its full manifest,
        the export gets the chapters the user asked for, and a crash mid-master
        leaves the original file back in place.
        """
        path = store.manifest_path(slug)
        original: str | None = None
        try:
            script = store.load_script(slug)
            included = store.included_chapters(slug, script)
            with open(path, encoding="utf-8") as fh:
                original = fh.read()
            manifest = json.loads(original)
            chapters = manifest.get("chapters") or []
            keep = [c for c in chapters if int(c.get("index", -1)) in included]
            if len(keep) == len(chapters):
                original = None  # nothing to hide; leave the file alone
            else:
                dropped = [c.get("index") for c in chapters if c not in keep]
                self.log(
                    job,
                    f"excluding chapter(s) {', '.join(str(i) for i in dropped)} from the export "
                    f"(include_in_audio=false)",
                )
                manifest["chapters"] = keep
                manifest["utterances"] = [
                    u for u in (manifest.get("utterances") or [])
                    if int(u.get("chapter_index", -1)) in included
                ]
                store._write_json(path, json.dumps(manifest, indent=2, ensure_ascii=False))
        except (OSError, ValueError, store.NotFound):
            original = None  # no manifest, or unreadable: master_book will say so
        try:
            yield
        finally:
            if original is not None:
                store._write_json(path, original)

    def _record_export_files(
        self, slug: str, result: Any, loudness: str, sample_rate: int, bitrate: str | None
    ) -> list[dict]:
        """Persist what this run produced, one row per file, and return the rows."""
        entries = list(getattr(result, "files", None) or [])
        if not entries:
            entries = [{
                "file": result.file,
                "bytes": result.bytes,
                "duration_ms": getattr(result, "duration_ms", None),
                "measured": getattr(result, "measured", None),
            }]
        rows: list[dict] = []
        for raw in entries:
            entry = dict(raw) if isinstance(raw, dict) else _as_dict(raw)
            name = str(entry.get("file") or "")
            if not name:
                continue
            row = {
                "file": name,
                "url": f"{store.public_prefix()}/books/{slug}/exports/{name}",
                "bytes": int(entry.get("bytes") or 0),
                "format": str(entry.get("format") or getattr(result, "format", "") or ""),
                "loudness": loudness,
                "sample_rate": sample_rate,
                "bitrate": bitrate,
                "chapter": entry.get("chapter"),
                "duration_ms": entry.get("duration_ms"),
                "measured": entry.get("measured"),
                "checks": entry.get("checks"),
            }
            checks = row["checks"]
            if isinstance(checks, dict):
                row["compliance"] = "pass" if all(checks.values()) else "fail"
            store.record_export(
                slug, name, **{k: v for k, v in row.items() if k not in ("url", "file")}
            )
            rows.append(row)
        return rows

    @staticmethod
    def _compliance(result: Any, files: list[dict]) -> str:
        stated = getattr(result, "compliance", None)
        if stated in ("pass", "fail"):
            return str(stated)
        verdicts = [f.get("compliance") for f in files if f.get("compliance")]
        if not verdicts:
            return "pass"
        return "pass" if all(v == "pass" for v in verdicts) else "fail"


# ---- helpers ----------------------------------------------------------------

def _as_dict(value: Any) -> dict:
    """A per-file export result, whatever shape ``master_book`` returns it in.

    ``master_book`` returns an ``ExportResult`` dataclass whose ACX ``files``
    are plain dicts; reading a dataclass, a pydantic model or a dict alike keeps
    this independent of the exact shape.
    """
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _graft_previous_timings(slug: str, script) -> None:
    """Carry the last render's timings onto the working script.

    Without this, rendering only chapter 3 would write a manifest in which
    chapters 1, 2 and 4 have no audio at all — the player would lose them even
    though their WAVs are untouched on disk."""
    manifest = store.load_manifest(slug)
    if manifest is None:
        return
    spans = {u.id: u.audio for u in manifest.utterances if u.audio is not None}
    for u in script.utterances:
        if u.id in spans:
            u.audio = spans[u.id]
    chapters = {c.index: c for c in manifest.chapters}
    for chapter in script.chapters:
        previous = chapters.get(chapter.index)
        if previous is not None:
            chapter.audio_file = previous.audio_file
            chapter.duration_ms = previous.duration_ms


def _direct_scope(script, request: dict) -> list[str]:
    passages = scene_passages(script)
    scope = request.get("scope") or "all"
    if scope == "all":
        # "All" is every scene that will be heard. The render job already refuses
        # to synthesize an ``include_in_audio: false`` chapter, so directing one
        # buys a model call and a review row for audio that is never made.
        audible = {
            c.index for c in script.chapters if getattr(c, "include_in_audio", True)
        }
        return [p.scene_id for p in passages if p.chapter_index in audible]
    if scope == "chapters":
        wanted = set(request.get("chapters") or [])
        return [p.scene_id for p in passages if p.chapter_index in wanted]
    if scope == "scenes":
        wanted = set(request.get("scenes") or [])
        return [p.scene_id for p in passages if p.scene_id in wanted]
    if scope == "undirected":
        directed = {u.id for u in script.utterances if u.render.instruction}
        return [p.scene_id for p in passages if not set(p.line_ids) & directed]
    raise ValueError(f"unknown direct scope {scope!r}")


def _direct_chapter_rows(script, scene_ids: list[str]) -> list[JobChapter]:
    wanted = set(scene_ids)
    titles = {c.index: c.title for c in script.chapters}
    totals: dict[int, int] = {}
    for passage in scene_passages(script):
        if passage.scene_id in wanted:
            totals[passage.chapter_index] = totals.get(passage.chapter_index, 0) + 1
    return [
        JobChapter(index=i, title=titles.get(i, ""), total=n, status="queued")
        for i, n in sorted(totals.items())
    ]


def _merge(existing: Pass | None, fresh: Pass) -> Pass:
    """Fold a resumed run's scenes back into the pass it continues."""
    if existing is None:
        return fresh
    known = {s.scene_id for s in existing.scenes}
    existing.scenes.extend(s for s in fresh.scenes if s.scene_id not in known)
    existing.scope.scenes = list(dict.fromkeys(existing.scope.scenes + fresh.scope.scenes))
    existing.elapsed_s = round(existing.elapsed_s + fresh.elapsed_s, 3)
    existing.finished_at = fresh.finished_at
    from ..direct import recount

    recount(existing)
    return existing


# A single manager per process; the app wires it up at startup.
manager = JobManager()
