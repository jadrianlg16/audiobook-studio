/**
 * Mock studio, entirely in memory.
 *
 * It exists so every screen can be driven end to end with no studio service, no
 * gateway, no model: edits flip lines to `stale`, decisions and `apply` write
 * direction into the script, and jobs advance on a timer and push the same three
 * SSE event shapes the contract defines.
 *
 * Shapes here are the ones in docs/STUDIO-API.md **including Amendments v1.1**.
 * Where the fixture makes a claim about the world (which engines exist, what
 * they can do) it is a plausible fixture, not a reading of the deployment.
 *
 * Chapter and scene indices are **0-based**, as the live API returns them. The
 * previous fixture numbered them from 1, which hid the desk's off-by-one.
 *
 * This module is loaded by a dynamic import(), so it is a separate chunk and
 * never reaches anyone who does not ask for ?mock=1.
 */
import { CASTING_MODES, isCastingMode, isResumableJob, isTerminalJob } from "../domain";
import type { StudioApi } from "./client";
import { ApiError } from "./http";
import type {
  Attribution,
  AudioState,
  BookSummary,
  CastEntry,
  CastingMode,
  CastSummary,
  Chapter,
  ChapterKind,
  ChapterPatch,
  DecideRequest,
  DirectRequest,
  DirectionSource,
  DirectorModels,
  EngineOption,
  ExportFile,
  ExportRequest,
  Health,
  Job,
  JobChapter,
  JobKind,
  JobLogEntry,
  JobStreamHandlers,
  JobSummary,
  Lexicon,
  Line,
  LineKind,
  LinePatch,
  NewBookRequest,
  Pass,
  PassScene,
  PassSummary,
  QcStatus,
  RenderRequest,
  Scene,
  Script,
  Settings,
  SettingsPatch,
  Slug,
  VersionEntry,
} from "./types";

// ---------------------------------------------------------------- utilities

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const nowIso = () => new Date().toISOString();
const iso = (daysAgo: number, hour = 9) => {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  d.setHours(hour, 14, 0, 0);
  return d.toISOString();
};

/** Deterministic 12-hex "cache key" so the stale key diff looks real. */
function hashKey(seed: string): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < seed.length; i++) {
    h1 = Math.imul(h1 ^ seed.charCodeAt(i), 16777619) >>> 0;
    h2 = Math.imul(h2 + seed.charCodeAt(i) * (i + 7), 2246822519) >>> 0;
  }
  return (h1.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0")).slice(0, 12);
}

const words = (s: string) => s.trim().split(/\s+/).filter(Boolean).length;
/** ~2.6 words a second, plus a beat of air, is close enough for a fixture. */
const speakMs = (s: string) => Math.round((words(s) / 2.6) * 1000) + 350;

const LICENCE_NOTE =
  "This book routes voices through non-commercial engine(s): breeze. Breeze TTS 2 weights are " +
  "research / non-commercial, and that covers self-hosted output — personal use only, not a " +
  "commercially published audiobook.";

// ---------------------------------------------------------------- engines

const KOKORO_VOICES = [
  { id: "af_heart", label: "Heart (US female)" },
  { id: "af_bella", label: "Bella (US female)" },
  { id: "af_nicole", label: "Nicole (US female)" },
  { id: "am_michael", label: "Michael (US male)" },
  { id: "am_adam", label: "Adam (US male)" },
  { id: "bf_emma", label: "Emma (UK female)" },
];

// The four events the open weights attest (docs/BREEZE-AUDIT-2026-09-05.md).
const BREEZE_TAGS = ["(laugh)", "(cough)", "(sigh)", "(clears throat)"];

const ENGINES: EngineOption[] = [
  {
    id: "kokoro",
    name: "Kokoro",
    available: true,
    instruction: false,
    audio_tags: [],
    voices: KOKORO_VOICES,
    license: "Apache-2.0",
    non_commercial: false,
  },
  {
    id: "breeze",
    name: "Breeze TTS 2",
    available: false, // the container is not running; capabilities still known
    instruction: true,
    audio_tags: BREEZE_TAGS,
    voices: [], // voice by description, not by name
    license: "Research / non-commercial",
    non_commercial: true,
  },
  {
    id: "indextts2",
    name: "IndexTTS2",
    available: false,
    instruction: true,
    audio_tags: [],
    voices: [{ id: "ref", label: "Reference clip" }],
    license: "Apache-2.0",
    non_commercial: false,
  },
  {
    id: "styletts2",
    name: "StyleTTS2",
    available: false,
    instruction: false,
    audio_tags: [],
    voices: [{ id: "default", label: "Default" }],
    license: "MIT code; pretrained-model terms: disclose synthetic speech",
    non_commercial: false,
  },
  {
    id: "chatterbox",
    name: "Chatterbox",
    available: false,
    instruction: false,
    audio_tags: [],
    voices: [{ id: "default", label: "Default" }],
    license: "MIT",
    non_commercial: false,
  },
];

const engineById = (id: string) => ENGINES.find((e) => e.id === id);
const takesDirection = (id: string) => engineById(id)?.instruction ?? false;

// ---------------------------------------------------------------- seeds

interface LineSeed {
  speaker: string;
  kind: LineKind;
  text: string;
  instruction?: string;
  source?: DirectionSource;
  spoken_text?: string;
  state: AudioState;
  flag?: boolean;
  note?: string;
  attribution?: Attribution;
  /**
   * Overrides the cast entry's voice. The fixture uses it for a line carrying a
   * voice its engine does not list — the state a book lands in when a character
   * is recast, and the one case the inspector's "(not on kokoro)" option exists
   * for.
   */
  voice_id?: string | null;
}
type SceneSeed = LineSeed[];
interface ChapterSeed {
  title: string;
  scenes: SceneSeed[];
  kind?: ChapterKind;
  include_in_audio?: boolean;
  /** v1.2 chapter sign-off. Absent = "unheard". */
  qc_status?: QcStatus;
}
interface BookSeed {
  slug: Slug;
  title: string;
  author: string;
  book_type: string;
  profile_label: string;
  language: string;
  cast: CastEntry[];
  chapters: ChapterSeed[];
  created_at: string;
  updated_at: string;
  rendered_at: string | null;
  /** v1.1: the last render came from the fake synth. */
  synthetic?: boolean;
  lexicon?: Record<string, string>;
}

const N = (text: string, state: AudioState, instruction?: string): LineSeed => ({
  speaker: "Narrator",
  kind: "narration",
  text,
  instruction,
  source: instruction ? "director" : "none",
  state,
});
const D = (speaker: string, text: string, state: AudioState, instruction?: string): LineSeed => ({
  speaker,
  kind: "dialogue",
  text,
  instruction,
  source: instruction ? "director" : "none",
  state,
});

const COLD_LANTERN: BookSeed = {
  slug: "cold-lantern",
  title: "The Cold Lantern",
  author: "H. M. Ashgrove",
  book_type: "literary_fiction",
  profile_label: "Literary fiction",
  language: "en",
  created_at: iso(9),
  updated_at: iso(0, 11),
  rendered_at: iso(1, 20),
  lexicon: { Elara: "eh-LAR-uh", harbour: "HAR-ber" },
  cast: [
    {
      character: "Narrator",
      voice_id: "af_heart",
      engine: "kokoro",
      notes: "Even, unhurried. Carries the book.",
      voice_design: null,
    },
    {
      character: "Elara",
      voice_id: null,
      engine: "breeze",
      notes: "Late thirties, coastal, holds a grudge quietly.",
      voice_design: "A woman in her late thirties, low and dry, clipped consonants.",
    },
    {
      character: "Marcus",
      voice_id: null,
      engine: "breeze",
      notes: "Older brother. Tired rather than defensive.",
      voice_design: "A man in his forties, warm but worn, speaks slightly under the beat.",
    },
    {
      character: "Unknown",
      voice_id: "af_nicole",
      engine: "kokoro",
      notes: "Lines the attributor would not guess. Reassign these before rendering.",
      voice_design: null,
    },
  ],
  chapters: [
    {
      title: "Arrival",
      qc_status: "approved",
      scenes: [
        [
          {
            // The inspector's voice select has an option for exactly this: a
            // voice kept across a recast that the engine does not list. Without
            // it the row read "— none —" while still rendering on af_sky.
            speaker: "Narrator",
            kind: "narration",
            text: "The lantern on the harbour wall had not been lit in a year.",
            state: "cached",
            voice_id: "af_sky",
          },
          N(
            "He counted the boats out of habit, and there were four.",
            "cached",
            "plain reporting, no weather in the voice",
          ),
          N("There was salt on every window on that side of the house.", "cached"),
        ],
        [
          N(
            "The stairs complained under him the way old wood does.",
            "cached",
            "slower here; the house is doing the talking",
          ),
          D(
            "Elara",
            "“The kettle is still warm. I heard the gate.”",
            "cached",
            "steady, almost gentle; she has decided something",
          ),
          D(
            "Marcus",
            "“I came as soon as the road was open.”",
            "cached",
            "tired, a little amused at himself",
          ),
        ],
      ],
    },
    {
      title: "The Letter",
      qc_status: "pickups",
      scenes: [
        [
          N("Morning came in grey and stayed that way until noon.", "cached"),
          N(
            "The clock in the hall ran four minutes fast and always had.",
            "cached",
            "dry, factual, faintly fond",
          ),
          D(
            "Elara",
            "“I kept the light on for a while. Then I stopped.”",
            "cached",
            "let the second sentence drop away rather than land",
          ),
        ],
        [
          D(
            "Marcus",
            "“Then say it plainly and I'll believe you.”",
            "cached",
            "no challenge in it; he means it",
          ),
          N(
            "She folded the map along its old creases and put it away.",
            "cached",
            "unhurried, hands busy",
          ),
        ],
      ],
    },
    {
      title: "The Cold Room",
      scenes: [
        [
          {
            // The desk's first row: a hand-written note on a line the director pass
            // could not touch, because kokoro takes no written direction.
            speaker: "Narrator",
            kind: "narration",
            text: "She put the letter down and did not pick it up again.",
            instruction: "quiet, resigned, unhurried — hold the last word",
            source: "manual",
            state: "cached",
          },
          D(
            "Elara",
            "“You're late,” she said, not turning around.",
            "stale",
            "colder now, almost clipped",
          ),
          D("Marcus", "“Three years late.”", "cached", "flat; no apology in it"),
          {
            // The attributor abstained: nobody is named and the alternation is
            // broken by the narration above. This is the row the QC filter finds.
            speaker: "Unknown",
            kind: "dialogue",
            text: "“Am I?” The voice came from the doorway.",
            state: "cached",
            flag: true,
            note: "Whose line is this? Sounds like Marcus in the manuscript.",
            attribution: { basis: "alternation", confidence: 0.4 },
          },
          N("Outside, the rain had stopped, and the street was very quiet.", "cached"),
        ],
        [
          {
            speaker: "Elara",
            kind: "dialogue",
            text: "“Don't tell me what the harbour master said. Tell me what you did.”",
            instruction: "pressure without volume; she is not going to repeat it",
            source: "director",
            state: "stale",
            attribution: { basis: "continuation", confidence: 0.62 },
          },
          D(
            "Marcus",
            "“The boat is gone, Elara. There's nothing to go back to.”",
            "cached",
            "plainly, as if reading a list",
          ),
          N("The tide went out and left the pier standing on its legs.", "missing"),
        ],
        [
          D(
            "Elara",
            "“I'm not angry. I'd like you to understand that first.”",
            "missing",
            "even, deliberate, one beat between the sentences",
          ),
          N("Nobody in the town used the north road after dark.", "cached"),
          D(
            "Marcus",
            "“I thought about the lantern every night. That's all I have.”",
            "cached",
            "quieter each clause; let it run out",
          ),
        ],
      ],
    },
    {
      title: "The Harbour",
      scenes: [
        [
          N("The kettle was still warm, which meant she had heard him coming.", "cached"),
          N("The fog came up the hill in one piece, like something being poured.", "cached"),
          D("Elara", "“You can put your coat by the fire. It won't dry.”", "cached"),
        ],
        [
          N("They walked out to the wall and neither of them said the year out loud.", "cached"),
          D("Marcus", "“It still turns. Somebody has been oiling it.”", "cached"),
        ],
      ],
    },
    {
      title: "What the Light Was For",
      scenes: [
        [
          N(
            "In the end the lantern was lit at four in the afternoon, badly, and stayed lit.",
            "missing",
          ),
          D("Elara", "“Leave it. It doesn't have to be tidy to work.”", "missing"),
          N("The town noticed on the second night, and said nothing on the third.", "missing"),
        ],
      ],
    },
  ],
};

const HARBOUR_ROAD: BookSeed = {
  slug: "harbour-road",
  title: "The Harbour Road",
  author: "H. M. Ashgrove",
  book_type: "general_fiction",
  profile_label: "General fiction",
  language: "en",
  created_at: iso(2),
  updated_at: iso(0, 8),
  rendered_at: null,
  cast: [
    {
      character: "Narrator",
      voice_id: "bf_emma",
      engine: "kokoro",
      notes: "Single voice, clean read.",
      voice_design: null,
    },
  ],
  chapters: [
    {
      title: "One",
      scenes: [
        [
          N(
            "The road out of the town runs along the water for two miles and then turns inland.",
            "missing",
          ),
          N("For most of the year it is the only way in.", "missing"),
          N("In August it is full of people who have never seen it in February.", "missing"),
        ],
      ],
    },
    {
      title: "Two",
      scenes: [
        [
          N("There were three families on the road and all of them fished.", "missing"),
          N("Now there is one, and it does not.", "missing"),
        ],
      ],
    },
    {
      title: "Three",
      scenes: [
        [
          N(
            "The bus comes twice a day and waits for nobody, which everyone considers fair.",
            "missing",
          ),
          N("You learn the timetable or you walk.", "missing"),
        ],
      ],
    },
  ],
};

const FOG_SIGNALS: BookSeed = {
  slug: "fog-signals",
  title: "A Short History of Fog Signals",
  author: "Ada Voss",
  book_type: "nonfiction",
  profile_label: "Non-fiction",
  language: "en",
  created_at: iso(21),
  updated_at: iso(3, 16),
  rendered_at: iso(3, 17),
  synthetic: true, // rendered while STUDIO_OFFLINE=1: not speech
  cast: [
    {
      character: "Narrator",
      voice_id: "am_adam",
      engine: "kokoro",
      notes: "Documentary register.",
      voice_design: null,
    },
  ],
  chapters: [
    {
      // v1.1: ingest marks a leading copyright/dedication page as front matter
      // and leaves it out of the audio. The rail says "skipped" rather than
      // silently narrating it as chapter one.
      title: "Front matter",
      kind: "front_matter",
      include_in_audio: false,
      scenes: [
        [
          N("Copyright 2026 by Ada Voss. All rights reserved.", "missing"),
          N("For everyone who ever waited up.", "missing"),
        ],
      ],
    },
    {
      title: "Bells and Guns",
      scenes: [
        [
          N(
            "Before the horn there was the bell, and before the bell there was a man with a gun.",
            "cached",
          ),
          N("Each of them answers the same question: where is the rock.", "cached"),
        ],
      ],
    },
    {
      title: "The Diaphone",
      scenes: [
        [
          N(
            "The diaphone's two-tone blast was designed to carry through weather that swallows a bell.",
            "cached",
          ),
          N("Its second, lower note is the part a sailor hears first.", "cached"),
        ],
      ],
    },
  ],
};

/**
 * A book long enough to need the row window.
 *
 * 1,200 lines in one chapter is the case the desk used to fall over on: every
 * row carries three buttons and an editable note, so rendering the lot cost
 * seconds of first paint and made every keystroke re-layout the column. It is
 * generated rather than written out so the fixture stays readable, and it is
 * deterministic so two runs produce the same script (and the same cache keys).
 */
const WATCH_LINES = [
  "The light turned once every eight seconds and the sea gave nothing back.",
  "He wrote the hour in the book, and then the weather, and then nothing.",
  "Somewhere below, the generator changed its mind about the pitch it was holding.",
  "The glass was cold enough to hurt and he kept his hand on it anyway.",
  "Gulls came up the cliff on the wind and went over the lantern without a sound.",
  "There was a ship at the edge of things that had not moved in an hour.",
  "The stairs were ninety-one, and he had counted them wrong twice that week.",
  "Rain arrived the way it does out there, all at once and from the side.",
  "He put the kettle on because it was the only decision left to make.",
  "The radio said the same three words and then went back to hissing.",
  "Salt had found the hinges again, and the door complained about it.",
  "By four the horizon was a rumour and the light was doing all the work.",
];

const WATCH_DIALOGUE: [string, string][] = [
  ["Keeper", "“It is not the dark that gets you. It is the routine.”"],
  ["Visitor", "“How long before someone comes out to you?”"],
  ["Keeper", "“Long enough that you stop asking.”"],
  ["Visitor", "“And if the light goes?”"],
  ["Keeper", "“Then I am the light, and I am slower.”"],
];

function longChapter(title: string, count: number, perScene = 12): ChapterSeed {
  const scenes: SceneSeed[] = [];
  let scene: SceneSeed = [];
  for (let i = 0; i < count; i++) {
    // Deterministic: every 9th line is dialogue, every 40th is flagged, every
    // 17th is stale, every 61st was never rendered.
    const state: AudioState = i % 61 === 60 ? "missing" : i % 17 === 16 ? "stale" : "cached";
    if (i % 9 === 8) {
      const [speaker, text] = WATCH_DIALOGUE[((i / 9) % WATCH_DIALOGUE.length) | 0];
      scene.push({
        speaker,
        kind: "dialogue",
        text,
        state,
        instruction: i % 27 === 8 ? "hold the pause before the last word" : undefined,
        source: i % 27 === 8 ? "director" : undefined,
        flag: i % 40 === 8,
        note: i % 40 === 8 ? "breath lands on the wrong beat" : undefined,
      });
    } else {
      scene.push({
        speaker: "Narrator",
        kind: "narration",
        text: `${WATCH_LINES[i % WATCH_LINES.length]} (${i + 1})`,
        state,
      });
    }
    if (scene.length >= perScene) {
      scenes.push(scene);
      scene = [];
    }
  }
  if (scene.length) scenes.push(scene);
  return { title, scenes };
}

const LONG_WATCH: BookSeed = {
  slug: "long-watch",
  title: "The Long Watch",
  author: "H. M. Ashgrove",
  book_type: "literary_fiction",
  profile_label: "Literary fiction",
  language: "en",
  created_at: iso(30),
  updated_at: iso(0, 7),
  rendered_at: iso(2, 22),
  cast: [
    {
      character: "Narrator",
      voice_id: "af_heart",
      engine: "kokoro",
      notes: "f",
      voice_design: null,
    },
    {
      character: "Keeper",
      voice_id: null,
      engine: "breeze",
      notes: "m",
      voice_design: "A man past sixty, unhurried, the voice of someone who talks to himself.",
    },
    {
      character: "Visitor",
      voice_id: "bf_emma",
      engine: "kokoro",
      notes: "n",
      voice_design: null,
    },
  ],
  chapters: [
    // 1,200 lines: over the 150-row threshold, so the desk windows it.
    { ...longChapter("The Long Watch", 1200), qc_status: "pickups" },
    // A short one right beside it, so switching chapters switches the desk
    // between the windowed and the plain path.
    {
      title: "After",
      qc_status: "approved",
      scenes: [
        [
          N(
            "The relief came on a Tuesday, in a boat that smelled of diesel and oranges.",
            "cached",
          ),
          N("He signed the book, and closed it, and that was the whole ceremony.", "cached"),
          D(
            "Keeper",
            "“It keeps turning whether you watch it or not.”",
            "cached",
            "flat, almost cheerful; he has decided not to be sad about it",
          ),
        ],
      ],
    },
  ],
};

// ---------------------------------------------------------------- building

function defaultAttribution(seed: LineSeed): Attribution {
  if (seed.attribution) return seed.attribution;
  if (seed.kind === "dialogue") return { basis: "tag", confidence: 0.92 };
  return { basis: "none", confidence: 1 };
}

function buildScript(seed: BookSeed): Script {
  const castByName = new Map(seed.cast.map((c) => [c.character, c]));
  const lines: Line[] = [];
  const chapters: Chapter[] = [];
  let order = 0;

  seed.chapters.forEach((chSeed, chapterIndex) => {
    const scenes: Scene[] = [];
    let cursor = 0; // ms inside the chapter
    let chDirected = 0;
    const chAudio = { cached: 0, stale: 0, missing: 0 };
    let chLines = 0;

    chSeed.scenes.forEach((scSeed, sceneIndex) => {
      const sceneId = `c${chapterIndex}_s${sceneIndex}`;
      const lineIds: string[] = [];

      scSeed.forEach((ls, i) => {
        const cast = castByName.get(ls.speaker) ?? seed.cast[0];
        // Semantics 4 of the contract: a director pass only stamps lines routed
        // to an instruction-capable engine. A manual note may sit anywhere.
        const source: DirectionSource = ls.source ?? (ls.instruction ? "director" : "none");
        const instruction =
          ls.instruction && (takesDirection(cast.engine) || source === "manual")
            ? ls.instruction
            : null;
        const id = `c${chapterIndex}_s${sceneIndex}_l${i}`;
        const dur = speakMs(ls.text);
        const voiceId = ls.voice_id !== undefined ? ls.voice_id : cast.voice_id;
        const cacheKey = hashKey(
          `${id}|${ls.text}|${instruction ?? ""}|${cast.engine}|${voiceId ?? ""}`,
        );
        const rendered = ls.state !== "missing";
        const line: Line = {
          id,
          order: order++,
          chapter_index: chapterIndex,
          scene_index: sceneIndex,
          speaker: ls.speaker,
          kind: ls.kind,
          text: ls.text,
          intent: { emotion: "neutral", intensity: 0.5, pace: 1 },
          render: {
            engine: cast.engine,
            voice_id: voiceId,
            speed: 1,
            lead_pause_ms: 0,
            trail_pause_ms: ls.kind === "dialogue" ? 220 : 320,
            instruction,
            spoken_text: ls.spoken_text ?? null,
            direction_source: instruction ? source : "none",
            voice_design: cast.voice_design,
          },
          audio: rendered
            ? {
                start_ms: cursor,
                end_ms: cursor + dur,
                sample_rate: 24000,
                cache_key: cacheKey,
              }
            : null,
          audio_state: ls.state,
          duration_ms: rendered ? dur : null,
          flag: ls.flag ?? false,
          note: ls.note ?? "",
          attribution: defaultAttribution(ls),
        };
        if (rendered) cursor += dur + line.render.trail_pause_ms;
        lines.push(line);
        lineIds.push(id);
        chLines++;
        if (line.render.instruction) chDirected++;
        chAudio[line.audio_state]++;
      });

      scenes.push({
        id: sceneId,
        chapter_index: chapterIndex,
        scene_index: sceneIndex,
        line_ids: lineIds,
      });
    });

    const everRendered = chAudio.cached + chAudio.stale > 0;
    chapters.push({
      index: chapterIndex,
      title: chSeed.title,
      scenes,
      audio_file: everRendered ? `chapter_${String(chapterIndex).padStart(3, "0")}.wav` : null,
      duration_ms: everRendered ? cursor : 0,
      lines: chLines,
      directed: chDirected,
      audio: chAudio,
      fresh: chAudio.stale === 0 && chAudio.missing === 0,
      kind: chSeed.kind ?? "chapter",
      include_in_audio: chSeed.include_in_audio ?? true,
      qc_status: chSeed.qc_status ?? "unheard",
    });
  });

  const script: Script = {
    book: {} as BookSummary, // filled by recompute()
    version: 7,
    profile: { id: seed.book_type, label: seed.profile_label },
    cast: clone(seed.cast),
    chapters,
    lines,
  };
  script.book = summarise(seed, script, new Set());
  return script;
}

function summarise(seed: BookSeed, script: Script, dirty: Set<number>): BookSummary {
  const engines: Record<string, number> = {};
  const audio = { cached: 0, stale: 0, missing: 0 };
  let directed = 0;
  let directable = 0;
  let wordCount = 0;
  for (const l of script.lines) {
    engines[l.render.engine] = (engines[l.render.engine] ?? 0) + 1;
    audio[l.audio_state]++;
    if (l.render.instruction) directed++;
    if (takesDirection(l.render.engine)) directable++;
    wordCount += words(l.text);
  }
  const included = script.chapters.filter((c) => c.include_in_audio);
  const includedIdx = new Set(included.map((c) => c.index));
  // v1.2: sign-off counts cover the chapters that are actually in the audio.
  const qc = {
    approved: included.filter((c) => c.qc_status === "approved").length,
    pickups: included.filter((c) => c.qc_status === "pickups").length,
    chapters: included.length,
  };
  const duration = included.reduce((a, c) => a + c.duration_ms, 0);
  const nonCommercial = Object.keys(engines).some((id) => engineById(id)?.non_commercial);
  const staleChapters = included.filter((c) => !c.fresh || dirty.has(c.index)).length;
  // Lines in a skipped chapter are never rendered, so they are not work.
  const needsLines = script.lines.filter(
    (l) => l.audio_state !== "cached" && includedIdx.has(l.chapter_index),
  ).length;
  return {
    slug: seed.slug,
    title: seed.title,
    author: seed.author,
    book_type: seed.book_type,
    profile_label: seed.profile_label,
    language: seed.language,
    chapters: script.chapters.length,
    utterances: script.lines.length,
    words: wordCount,
    est_minutes: Math.round((wordCount / 155) * 10) / 10,
    duration_ms: seed.rendered_at ? duration : 0,
    engines,
    directed: { directed, total: script.lines.length },
    audio,
    cast: clone(script.cast),
    created_at: seed.created_at,
    updated_at: seed.updated_at,
    rendered_at: seed.rendered_at,
    last_job: null,
    status: "idle",
    licence_note: nonCommercial ? LICENCE_NOTE : null,
    exportable: staleChapters === 0 && needsLines === 0,
    directable: { lines: directable, total: script.lines.length },
    needs_render: { lines: needsLines, chapters: staleChapters },
    synthetic: seed.synthetic ?? false,
    qc,
  };
}

// ---------------------------------------------------------------- state

interface BookState {
  seed: BookSeed;
  script: Script;
  passes: Pass[];
  exports: ExportFile[];
  versions: VersionEntry[];
  lexicon: Record<string, string>;
  /**
   * Chapters whose WAV has not been re-stitched since a line changed. A per-line
   * re-render leaves exactly this state: every line cached, the chapter not
   * fresh — which used to present as "Export is disabled and nothing says why".
   */
  dirty: Set<number>;
}

const books = new Map<Slug, BookState>();

function seedBook(seed: BookSeed) {
  books.set(seed.slug, {
    seed,
    script: buildScript(seed),
    passes: [],
    exports: [],
    versions: [],
    lexicon: { ...(seed.lexicon ?? {}) },
    dirty: new Set(),
  });
  const b = books.get(seed.slug)!;
  b.versions = Array.from({ length: b.script.version }, (_, i) => ({
    version: i + 1,
    saved_at: iso(9 - i > 0 ? 9 - i : 0, 9 + (i % 8)),
    note:
      i === 0
        ? "ingested"
        : i === b.script.version - 1
          ? "manual direction on 1 line"
          : "cast / direction edits",
  })).reverse();
  recompute(seed.slug);
}

for (const seed of [COLD_LANTERN, HARBOUR_ROAD, FOG_SIGNALS, LONG_WATCH]) seedBook(seed);

/**
 * What the studio would answer: an ApiError with its status, and a `url` that
 * says it came from the mock (nothing was requested).
 */
const fail = (status: number, detail: string, where = "") =>
  new ApiError(status, detail, `mock:/api${where}`);

function book(slug: Slug): BookState {
  const b = books.get(slug);
  if (!b) throw fail(404, `No book "${slug}"`, `/books/${slug}`);
  return b;
}

/** Re-derive every count on the summary after the script changed. */
function recompute(slug: Slug) {
  const b = book(slug);
  for (const ch of b.script.chapters) {
    const audio = { cached: 0, stale: 0, missing: 0 };
    let directed = 0;
    const ids = new Set(ch.scenes.flatMap((s) => s.line_ids));
    for (const l of b.script.lines) {
      if (!ids.has(l.id)) continue;
      audio[l.audio_state]++;
      if (l.render.instruction) directed++;
    }
    ch.audio = audio;
    ch.directed = directed;
    ch.fresh = audio.stale === 0 && audio.missing === 0 && !b.dirty.has(ch.index);
    ch.qc_status = ch.qc_status ?? "unheard";
  }
  const prev = b.script.book;
  b.script.book = {
    ...summarise(b.seed, b.script, b.dirty),
    last_job: prev?.last_job ?? null,
    status: prev?.status ?? "idle",
  };
}

function saveVersion(slug: Slug, note: string) {
  const b = book(slug);
  b.script.version += 1;
  b.versions.unshift({ version: b.script.version, saved_at: nowIso(), note });
  b.seed.updated_at = nowIso();
}

function markStale(line: Line, seedExtra: string) {
  if (line.audio_state === "cached") line.audio_state = "stale";
  if (line.audio) {
    line.audio = {
      ...line.audio,
      cache_key: hashKey(`${line.id}|${line.text}|${seedExtra}|${line.render.engine}`),
    };
  }
}

/** The casting rule a new character goes through (v1.1 PATCH line {speaker}). */
function castFor(b: BookState, character: string): CastEntry {
  const existing = b.script.cast.find((c) => c.character === character);
  if (existing) return existing;
  const used = new Set(b.script.cast.map((c) => c.voice_id).filter(Boolean) as string[]);
  const free = KOKORO_VOICES.find((v) => !used.has(v.id)) ?? KOKORO_VOICES[0];
  const entry: CastEntry = {
    character,
    voice_id: settings.narrator_engine === "kokoro" ? free.id : null,
    engine: settings.narrator_engine,
    notes: "Created by a speaker reassignment.",
    voice_design: null,
  };
  b.script.cast.push(entry);
  b.seed.cast.push(clone(entry));
  return entry;
}

// ---------------------------------------------------------------- passes

const DIRECTOR_NOTES = [
  "quiet, resigned, unhurried — hold the last word",
  "colder now, almost clipped",
  "flat; no apology in it",
  "steady, almost gentle; she has decided something",
  "let the second sentence drop away rather than land",
  "plain reporting, no weather in the voice",
  "tired, a little amused at himself",
  "even, deliberate, one beat between the sentences",
  "pressure without volume; she is not going to repeat it",
  "slower here; the house is doing the talking",
];

const SUMMARIES = [
  "Two people avoiding the subject they came to discuss.",
  "A room that has been waiting a long time to be walked into.",
  "The argument finally arrives, and it is very quiet.",
  "Weather doing the work the characters will not.",
  "An apology that never uses the word.",
];

/** v1.1: context for the human, never sent to an engine. */
const SCENE_NOTES = [
  "An exchange between Elara and Marcus: the room stays quiet underneath all of it.",
  "Narration only. The house is the third character in this scene.",
  "She has already decided; he is still arguing with a version of her from three years ago.",
  "Weather and objects. Nobody says the thing.",
  "The apology, such as it is. Neither of them names it.",
];

/** The pass the review screen opens on. */
function buildPass(slug: Slug): Pass {
  const b = book(slug);
  const allScenes = b.script.chapters.flatMap((c) =>
    c.scenes.map((s) => ({ scene: s, chapter: c })),
  );
  const scenes: PassScene[] = [];

  // The two scenes the validator threw away, with the model's actual text so
  // the diff is real rather than decorative.
  const rejected: Record<string, { source: string; model: string; tags: Record<string, number> }> =
    {
      c2_s0: {
        source: "She put the letter down and did not pick it up again.",
        model: "She set the letter down and did not pick it up again. (sigh)",
        tags: { "(sigh)": 1 },
      },
      c1_s1: {
        source:
          "“Then say it plainly and I'll believe you.” She folded the map along its old creases and put it away.",
        model:
          "“Then say it plainly, and I will believe you.” (sigh) She folded the map along its creases and put it away.",
        tags: { "(sigh)": 1 },
      },
    };
  const ignoredScene = "c4_s0";
  const promptVersion = "director/2026-08-30";

  // 42 scenes in the pass: the book's real scenes, then filler scene ids so the
  // counts match what the CLI prints for a book this size.
  const target = 42;
  for (let i = 0; i < target; i++) {
    const real = allScenes[i];
    const chapterIndex = real ? real.chapter.index : i % 5;
    const sceneIndex = real ? real.scene.scene_index : Math.floor(i / 5);
    const sceneId = real ? real.scene.id : `c${chapterIndex}_s${sceneIndex + 3}`;
    const chapterTitle = real
      ? real.chapter.title
      : (b.script.chapters[chapterIndex]?.title ?? "Chapter");
    const sourceText = real
      ? real.scene.line_ids
          .map((id) => b.script.lines.find((l) => l.id === id)?.text ?? "")
          .join(" ")
      : "The passage continues in the same register for another page.";

    const rej = rejected[sceneId];
    const isIgnored = sceneId === ignoredScene;
    const status = rej ? "rejected" : isIgnored ? "ignored" : "kept";
    const instruction = isIgnored ? null : DIRECTOR_NOTES[i % DIRECTOR_NOTES.length];
    const tagsUsed = rej ? rej.tags : i % 5 === 0 ? { "(sigh)": 1 } : {};

    scenes.push({
      scene_id: sceneId,
      chapter_index: chapterIndex,
      scene_index: sceneIndex,
      chapter_title: chapterTitle,
      status,
      instruction,
      tags_status: rej ? "rejected" : Object.keys(tagsUsed).length ? "kept" : "none",
      tags_used: tagsUsed,
      source_text: rej ? rej.source : sourceText,
      model_text: rej ? rej.model : null,
      line_spoken_text: null,
      summary: SUMMARIES[i % SUMMARIES.length],
      emotion: ["neutral", "sombre", "tense", "wistful"][i % 4],
      scalar: Number((0.4 + (i % 5) * 0.08).toFixed(2)),
      decision: "pending",
      final_instruction: null,
      attempts: 1,
      error: null,
      // v1.4: why the validator threw a scene away, and which prompt wrote it.
      reason: rej
        ? "the annotated text was not the source passage"
        : isIgnored
          ? "the director returned nothing usable for this engine"
          : null,
      prompt_version: promptVersion,
      scene_note: isIgnored ? null : SCENE_NOTES[i % SCENE_NOTES.length],
      pace: isIgnored ? null : Number((0.9 + (i % 4) * 0.05).toFixed(2)),
    });
  }

  const counts = {
    kept: scenes.filter((s) => s.status === "kept").length,
    rejected: scenes.filter((s) => s.status === "rejected").length,
    ignored: scenes.filter((s) => s.status === "ignored").length,
    error: 0,
    scenes: scenes.length,
  };

  return {
    id: "pass_9f2ca41",
    book: slug,
    created_at: iso(0, 10),
    finished_at: iso(0, 10),
    provider: "ollama",
    model: "gemma3:12b",
    prompt_version: promptVersion,
    target_engine: "breeze",
    scope: { scenes: scenes.map((s) => s.scene_id) },
    counts,
    elapsed_s: 192,
    scenes,
    applied_at: null,
  };
}

/** The ACX columns of an export row that is not ACX: present, and null (v1.4). */
const NOT_ACX = {
  chapter: null,
  duration_ms: null,
  bitrate: null,
  measured: null,
  checks: null,
  compliance: null,
} as const;

/** ACX exports are 192 kbps CBR (STUDIO-API.md, Amendments v1.1). */
const ACX_BITRATE = "192k";

books.get("cold-lantern")!.passes.push(buildPass("cold-lantern"));
books.get("cold-lantern")!.exports.push({
  file: "the-cold-lantern.m4b",
  url: "/studio/books/cold-lantern/exports/the-cold-lantern.m4b",
  bytes: 48_213_004,
  created_at: iso(1, 21),
  format: "m4b",
  loudness: "acx",
  sample_rate: 44100,
  ...NOT_ACX,
});
// A v1.1 ACX export: one MP3 per included chapter, with its compliance row.
books.get("cold-lantern")!.exports.unshift(
  ...[0, 1].map((ch) => ({
    file: `the-cold-lantern-${String(ch).padStart(3, "0")}.mp3`,
    url: `/studio/books/cold-lantern/exports/the-cold-lantern-${String(ch).padStart(3, "0")}.mp3`,
    bytes: 9_120_400 + ch * 210_000,
    created_at: iso(1, 22),
    format: "acx",
    loudness: "acx",
    sample_rate: 44100,
    chapter: ch,
    duration_ms: 372_000 + ch * 41_000,
    bitrate: ACX_BITRATE,
    measured: {
      integrated_lufs: Number((-19.1 + ch * 0.2).toFixed(1)),
      true_peak_dbtp: Number((-3.7 + ch * 0.1).toFixed(1)),
    },
    checks: { peak_ok: true, loudness_ok: true, duration_ok: true },
    compliance: "pass" as const,
  })),
);

// ---------------------------------------------------------------- jobs

interface JobState {
  job: Job;
  subs: Set<JobStreamHandlers>;
  timer: number | null;
  /** ticking starts the first time somebody looks at the job */
  started: boolean;
  onFinish?: () => void;
}

const jobs = new Map<string, JobState>();
let jobSeq = 0;

const TICK_MS = 600;

function jobId(kind: JobKind): string {
  jobSeq += 1;
  return `job_${kind.slice(0, 3)}_${String(jobSeq).padStart(3, "0")}`;
}

/** The wire's `progress` frames carry no log; its `done` frame is the whole Job. */
function frame(job: Job) {
  const { log: _log, ...rest } = job;
  return clone(rest);
}

function emit(js: JobState, type: "progress" | "log" | "done", payload?: JobLogEntry) {
  for (const h of js.subs) {
    if (type === "progress") h.onProgress(frame(js.job));
    else if (type === "done") h.onDone(clone(js.job));
    else if (payload) h.onLog(payload);
  }
}

function log(js: JobState, line: string) {
  const entry: JobLogEntry = { ts: nowIso(), line };
  js.job.log.push(entry);
  if (js.job.log.length > 200) js.job.log.shift();
  emit(js, "log", entry);
}

function stopTicker(js: JobState) {
  if (js.timer !== null) {
    window.clearInterval(js.timer);
    js.timer = null;
  }
}

function startTicker(js: JobState) {
  if (js.timer !== null || js.job.status !== "running") return;
  js.started = true;
  js.job.started_at ??= nowIso();
  js.timer = window.setInterval(() => tick(js), TICK_MS);
}

function tick(js: JobState) {
  const j = js.job;
  if (j.status !== "running") return;

  const step = Math.max(1, Math.ceil(j.progress.total / 28));
  const before = j.progress.done;
  j.progress.done = Math.min(j.progress.total, before + step);
  j.progress.elapsed_s += TICK_MS / 1000;

  // Cache hits are the headline number on the job screen; a re-render of a mostly cached
  // book should read high, a first render low.
  const fresh = j.progress.done - before;
  const hitRate = (j.request as { cache_bias?: number }).cache_bias ?? 0.68;
  const hits = Math.round(fresh * hitRate);
  j.progress.cache_hits += hits;
  j.progress.synth_calls += fresh - hits;
  j.progress.rtf = Number((0.28 + (j.progress.done % 7) * 0.01).toFixed(2));
  const rate = j.progress.done / Math.max(1, j.progress.elapsed_s);
  j.progress.eta_s = Math.round((j.progress.total - j.progress.done) / Math.max(0.1, rate));

  // walk the per-chapter rows
  let remaining = j.progress.done;
  for (const ch of j.chapters) {
    if (remaining <= 0) {
      ch.status = ch.done > 0 ? ch.status : ch.total === 0 ? "skipped" : "queued";
      continue;
    }
    const take = Math.min(ch.total, remaining);
    ch.done = take;
    remaining -= take;
    if (ch.total === 0) {
      ch.status = "skipped";
    } else if (ch.done >= ch.total) {
      if (ch.status !== "done") ch.seconds = Number((ch.seconds || 0).toFixed(0));
      ch.status = "done";
    } else {
      ch.status = "running";
      ch.seconds += TICK_MS / 1000;
    }
  }

  // log against the chapter actually being worked on, not a made-up index
  const active =
    j.chapters.find((c) => c.status === "running") ?? j.chapters[j.chapters.length - 1];
  const chIdx = active?.index ?? 0;
  const scIdx = j.progress.done % 3;
  const engine =
    j.kind === "render"
      ? (Object.keys(active?.engines ?? {})[
          j.progress.done % Math.max(1, Object.keys(active?.engines ?? {}).length)
        ] ?? "kokoro")
      : settings.director.model;
  if (j.kind === "direct") {
    log(js, `scene c${chIdx}_s${scIdx}  direct  ${engine}  kept`);
  } else if (j.kind === "export") {
    log(js, `mastering chapter_${String(chIdx).padStart(3, "0")}.wav  loudnorm  -19 LUFS`);
  } else {
    const key = hashKey(`${chIdx}-${j.progress.done}`);
    log(
      js,
      `c${chIdx}_s${scIdx}_l${j.progress.done % 4}  ` +
        `${hits > 0 ? "cache" : "synth"}  ${engine}  ${(0.9 + (j.progress.done % 9) * 0.06).toFixed(2)}s  ` +
        `→  ${key.slice(0, 4)}…${key.slice(-3)}`,
    );
  }

  emit(js, "progress");

  if (j.progress.done >= j.progress.total) {
    finish(js);
  }
}

function finish(js: JobState) {
  const j = js.job;
  stopTicker(js);
  j.status = "done";
  j.finished_at = nowIso();
  j.progress.eta_s = 0;
  for (const ch of j.chapters) {
    ch.done = ch.total;
    ch.status = ch.total === 0 ? "skipped" : "done";
  }
  js.onFinish?.();
  if (j.kind === "render" && !j.result) {
    const b = books.get(j.book);
    j.result = {
      chapters_written: j.chapters.filter((c) => c.total > 0).map((c) => c.index),
      duration_ms: b ? b.script.chapters.reduce((a, c) => a + c.duration_ms, 0) : 0,
    };
  }
  log(js, `${j.kind} finished in ${Math.round(j.progress.elapsed_s)}s`);
  emit(js, "progress");
  emit(js, "done");
}

function makeJob(
  kind: JobKind,
  slug: Slug,
  total: number,
  chapters: JobChapter[],
  request: Record<string, unknown>,
  onFinish?: () => void,
): Job {
  const id = jobId(kind);
  const job: Job = {
    id,
    kind,
    book: slug,
    status: "running",
    created_at: nowIso(),
    started_at: nowIso(),
    finished_at: null,
    progress: {
      done: 0,
      total,
      cache_hits: 0,
      synth_calls: 0,
      rtf: null,
      elapsed_s: 0,
      eta_s: null,
    },
    error: null,
    result: null,
    chapters,
    log: [{ ts: nowIso(), line: `${kind} queued for ${slug}` }],
    request,
  };
  const js: JobState = { job, subs: new Set(), timer: null, started: false, onFinish };
  jobs.set(id, js);
  const b = books.get(slug);
  if (b) {
    b.script.book.status =
      kind === "render" ? "rendering" : kind === "direct" ? "directing" : "exporting";
    b.script.book.last_job = summaryOf(job);
  }
  startTicker(js);
  return clone(job);
}

function summaryOf(j: Job): JobSummary {
  const { chapters: _c, log: _l, request: _r, ...rest } = j;
  return clone(rest);
}

function chapterRows(slug: Slug, onlyStale: boolean): { rows: JobChapter[]; total: number } {
  const b = book(slug);
  const rows: JobChapter[] = [];
  let total = 0;
  for (const ch of b.script.chapters) {
    const ids = new Set(ch.scenes.flatMap((s) => s.line_ids));
    const lines = b.script.lines.filter((l) => ids.has(l.id));
    const count = !ch.include_in_audio
      ? 0
      : onlyStale
        ? lines.filter((l) => l.audio_state !== "cached").length
        : lines.length;
    const engines: Record<string, number> = {};
    for (const l of lines) engines[l.render.engine] = (engines[l.render.engine] ?? 0) + 1;
    total += count;
    rows.push({
      index: ch.index,
      title: ch.title,
      done: 0,
      total: count,
      status: count === 0 ? "skipped" : "queued",
      seconds: 0,
      engines: count === 0 ? {} : engines,
    });
  }
  return { rows, total: Math.max(1, total) };
}

/** A render job that lands: every included line of the book becomes cached. */
function applyRender(slug: Slug, scope: RenderRequest["scope"]) {
  const b = book(slug);
  const skipped = new Set(b.script.chapters.filter((c) => !c.include_in_audio).map((c) => c.index));
  /** v1.2: a chapter whose audio just changed is unheard again, whatever it was. */
  const rerendered = new Set<number>();
  for (const l of b.script.lines) {
    if (skipped.has(l.chapter_index)) continue;
    if (scope === "stale" && l.audio_state === "cached") continue;
    rerendered.add(l.chapter_index);
    if (!l.audio) {
      l.audio = {
        start_ms: 0,
        end_ms: speakMs(l.text),
        sample_rate: 24000,
        cache_key: hashKey(`${l.id}|${l.text}|${l.render.instruction ?? ""}`),
      };
    }
    l.duration_ms = speakMs(l.text);
    l.audio_state = "cached";
  }
  // restack per-chapter timings so follow-along stays honest
  for (const ch of b.script.chapters) {
    if (!ch.include_in_audio) continue;
    let cursor = 0;
    for (const id of ch.scenes.flatMap((s) => s.line_ids)) {
      const l = b.script.lines.find((x) => x.id === id);
      if (!l || !l.audio) continue;
      const dur = l.duration_ms ?? speakMs(l.text);
      l.audio = { ...l.audio, start_ms: cursor, end_ms: cursor + dur };
      cursor += dur + l.render.trail_pause_ms;
    }
    ch.duration_ms = cursor;
    ch.audio_file = `chapter_${String(ch.index).padStart(3, "0")}.wav`;
  }
  for (const ch of b.script.chapters) {
    if (rerendered.has(ch.index)) ch.qc_status = "unheard";
  }
  // every chapter has been re-stitched, so nothing is left waiting for a rebuild
  b.dirty.clear();
  b.seed.rendered_at = nowIso();
  b.seed.synthetic = settings.offline;
  recompute(slug);
  b.script.book.status = "idle";
}

// One job is already in flight when the app loads, so /jobs/:id has something
// to show. Its clock starts the first time the job is opened.
const seededLive = (() => {
  const { rows, total } = chapterRows("harbour-road", false);
  const j = makeJob("render", "harbour-road", total, rows, { scope: "all", cache_bias: 0.12 }, () =>
    applyRender("harbour-road", "all"),
  );
  const js = jobs.get(j.id)!;
  stopTicker(js); // lazy: starts on first getJob/subscribe
  js.started = false;
  js.job.progress.done = 0;
  return j.id;
})();

// A finished render, so the shelf has a "last job" to link to.
const seededDone = (() => {
  const { rows, total } = chapterRows("cold-lantern", false);
  const j = makeJob("render", "cold-lantern", total, rows, { scope: "all" });
  const js = jobs.get(j.id)!;
  stopTicker(js);
  js.job.status = "done";
  js.job.progress.done = total;
  js.job.progress.cache_hits = Math.round(total * 0.68);
  js.job.progress.synth_calls = total - js.job.progress.cache_hits;
  js.job.progress.elapsed_s = 242;
  js.job.progress.rtf = 0.31;
  js.job.progress.eta_s = 0;
  js.job.started_at = iso(1, 19);
  js.job.created_at = iso(1, 19);
  js.job.finished_at = iso(1, 20);
  js.job.result = { chapters_written: [0, 1, 2, 3], duration_ms: 391_000 };
  for (const ch of js.job.chapters) {
    ch.done = ch.total;
    ch.status = ch.total === 0 ? "skipped" : "done";
    ch.seconds = 40 + ch.index * 12;
  }
  const b = book("cold-lantern");
  b.script.book.status = "idle";
  b.script.book.last_job = summaryOf(js.job);
  return j.id;
})();

// An interrupted job, so Resume (and the screen's re-subscribe) can be driven.
const seededInterrupted = (() => {
  const { rows, total } = chapterRows("fog-signals", false);
  const j = makeJob("render", "fog-signals", total, rows, { scope: "all", cache_bias: 0.55 });
  const js = jobs.get(j.id)!;
  stopTicker(js);
  js.started = false;
  js.job.status = "interrupted";
  js.job.progress.done = Math.max(1, Math.floor(total / 3));
  js.job.progress.cache_hits = 1;
  js.job.progress.elapsed_s = 34;
  js.job.log.push({ ts: iso(0, 9), line: "service restarted mid-job → interrupted" });
  js.onFinish = () => applyRender("fog-signals", "all");
  const b = book("fog-signals");
  b.script.book.status = "idle";
  b.script.book.last_job = summaryOf(js.job);
  return j.id;
})();

book("harbour-road").script.book.status = "rendering";

// ---------------------------------------------------------------- settings

let settings: Settings = {
  offline: true,
  fake_director: true,
  gateway_url: "http://gateway:8000",
  narrator_engine: "kokoro",
  expressive_engine: "breeze",
  casting_mode: "dialogue_expressive",
  director: { provider: "ollama", model: "gemma3:12b" },
  // 22050 is deliberately not one of the three options the settings screen
  // offers: it exercises the "(from settings)" fallback, which exists because a
  // stored rate outside the list used to display as 44100 while the draft still
  // held — and saved — the old value.
  export: { format: "m4b", loudness: "acx", sample_rate: 22050, bitrate: null },
  ffmpeg: true,
  book_types: [
    "general_fiction",
    "literary_fiction",
    "thriller",
    "romance",
    "sci_fi",
    "fantasy",
    "nonfiction",
    "memoir",
    "self_help",
    "childrens",
  ],
  engines: ENGINES,
};

const DIRECTOR_MODELS: DirectorModels = {
  provider: "ollama",
  models: [
    { name: "gemma3:12b", size_bytes: 8_149_190_656, modified_at: iso(30) },
    { name: "qwen3:30b", size_bytes: 18_557_235_200, modified_at: iso(12) },
    { name: "llama3.2:3b", size_bytes: 2_019_377_664, modified_at: iso(60) },
    { name: "mistral:7b", size_bytes: 4_113_301_824, modified_at: iso(45) },
  ],
  default: "gemma3:12b",
  error: null,
};

// ---------------------------------------------------------------- the api

const delay = <T>(v: T, ms = 140): Promise<T> =>
  new Promise((resolve) => window.setTimeout(() => resolve(v), ms));

/**
 * A read that can be cancelled, so `useAsync`'s AbortController does the same
 * thing here as it does against the real service: leaving the desk while the
 * script is loading rejects with an AbortError, which the caller ignores.
 */
const read = <T>(make: () => T, signal?: AbortSignal, ms = 140): Promise<T> =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(make());
    }, ms);
    function onAbort() {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });

function slugify(title: string): string {
  const base =
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "") || "book";
  if (!books.has(base)) return base;
  let n = 2;
  while (books.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

/**
 * A new book's narrator under the casting rule (pipeline/cast.py
 * assign_engines): all_expressive moves the narrator to the expressive engine,
 * the other two modes keep it on the narrator engine. A new mock book has no
 * other character yet, so that is the whole cast.
 */
function newBookNarrator(mode: CastingMode): CastEntry {
  if (mode !== "all_expressive") {
    return {
      character: "Narrator",
      voice_id: settings.narrator_engine === "kokoro" ? "af_heart" : null,
      engine: settings.narrator_engine,
      notes: "Single voice until casting runs.",
      voice_design: null,
    };
  }
  const engine = settings.expressive_engine;
  const voice = engineById(engine)?.voices[0]?.id ?? null;
  return {
    character: "Narrator",
    voice_id: voice,
    engine,
    notes: "Single voice until casting runs.",
    // An engine with no named voices is cast by description.
    voice_design: voice ? null : "An even, unhurried narrator, close to the microphone.",
  };
}

function newBookFrom(
  title: string,
  author: string,
  text: string,
  bookType: string,
  castingMode: CastingMode = settings.casting_mode,
): BookSummary {
  const slug = slugify(title || "Untitled");
  const paragraphs = text
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);
  const chunks = paragraphs.length ? paragraphs : ["(empty book)"];
  const perChapter = Math.max(1, Math.ceil(chunks.length / 3));
  const chapterSeeds: ChapterSeed[] = [];
  for (let i = 0; i < chunks.length; i += perChapter) {
    chapterSeeds.push({
      title: `Chapter ${chapterSeeds.length + 1}`,
      scenes: [chunks.slice(i, i + perChapter).map((t) => N(t, "missing"))],
    });
  }
  const seed: BookSeed = {
    slug,
    title: title || "Untitled",
    author: author || "Unknown",
    book_type: bookType === "auto" ? "general_fiction" : bookType,
    profile_label:
      bookType === "auto"
        ? "General fiction (detected)"
        : bookType.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()),
    language: "en",
    cast: [newBookNarrator(castingMode)],
    chapters: chapterSeeds,
    created_at: nowIso(),
    updated_at: nowIso(),
    rendered_at: null,
  };
  seedBook(seed);
  const b = book(slug);
  b.script.version = 1;
  b.versions = [{ version: 1, saved_at: nowIso(), note: "ingested" }];
  return clone(b.script.book);
}

function castSummary(slug: Slug): CastSummary {
  const b = book(slug);
  const lines: Record<string, number> = {};
  const first: Record<string, string> = {};
  for (const l of b.script.lines) {
    lines[l.speaker] = (lines[l.speaker] ?? 0) + 1;
    if (!(l.speaker in first)) first[l.speaker] = l.text;
  }
  for (const c of b.script.cast) {
    lines[c.character] ??= 0;
    first[c.character] ??= "";
  }
  const byVoice = new Map<string, string[]>();
  for (const c of b.script.cast) {
    if (!c.voice_id) continue;
    const key = `${c.engine}:${c.voice_id}`;
    byVoice.set(key, [...(byVoice.get(key) ?? []), c.character]);
  }
  return {
    cast: clone(b.script.cast),
    lines,
    first_line: first,
    shared_voices: [...byVoice.values()].filter((g) => g.length > 1),
  };
}

function lexiconOf(slug: Slug): Lexicon {
  const b = book(slug);
  const occurrences: Record<string, number> = {};
  const global: Record<string, string> = { Ashgrove: "ASH-grove" };
  for (const term of [...Object.keys(b.lexicon), ...Object.keys(global)]) {
    const re = new RegExp(`\\b${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i");
    occurrences[term] = b.script.lines.filter((l) => re.test(l.text)).length;
  }
  return { entries: { ...b.lexicon }, global, occurrences };
}

// ---------------------------------------------------------------- audio

/**
 * Chapter and line audio that plays with no studio behind the page: silence of
 * the take's real length, as an object URL, so the desk's transport and
 * follow-script run through a chapter the way they do over real audio. A
 * /studio/ URL here was a request to a server that is not there.
 *
 * 8-bit mono PCM, where silence is the midpoint (128). The rate is 8 kHz,
 * lowered for a long take to keep the file near AUDIO_BUDGET_BYTES, but never
 * below 3 kHz: Chrome will not decode a slower WAV (measured on Chrome with
 * Playwright, 2026-09-26: 1 kHz and below fail with "no supported streams").
 * The Long Watch's two-hour first chapter comes out at about 22 MB.
 */
const AUDIO_RATE = 8_000;
const AUDIO_MIN_RATE = 3_000;
const AUDIO_BUDGET_BYTES = 4_000_000;

function silentWav(ms: number): Blob {
  const seconds = Math.max(1, ms / 1000);
  const rate = Math.max(
    AUDIO_MIN_RATE,
    Math.min(AUDIO_RATE, Math.floor(AUDIO_BUDGET_BYTES / seconds)),
  );
  const samples = Math.round((rate * ms) / 1000);
  const buf = new ArrayBuffer(44 + samples);
  const view = new DataView(buf);
  const ascii = (at: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i));
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + samples, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate, true); // bytes per second: one byte per sample
  view.setUint16(32, 1, true); // block align
  view.setUint16(34, 8, true); // bits per sample
  ascii(36, "data");
  view.setUint32(40, samples, true);
  new Uint8Array(buf, 44).fill(128);
  return new Blob([buf], { type: "audio/wav" });
}

/** One object URL per take, kept for as long as the take's length holds. */
const audioUrls = new Map<string, { ms: number; url: string }>();

function silenceUrl(key: string, ms: number): string {
  const cached = audioUrls.get(key);
  if (cached?.ms === ms) return cached.url;
  if (cached) URL.revokeObjectURL(cached.url);
  const url = URL.createObjectURL(silentWav(ms));
  audioUrls.set(key, { ms, url });
  return url;
}

/**
 * Where the studio answers 404 (no such chapter file; no take at the line's
 * current settings): an empty WAV. It fails to decode, so the player shows
 * its "no audio" path, and it is never a request.
 */
const NO_AUDIO = "data:audio/wav;base64,";

function lineAudioUrl(slug: Slug, lineId: string): string {
  const line = books.get(slug)?.script.lines.find((l) => l.id === lineId);
  if (!line || line.audio_state !== "cached" || !line.duration_ms) return NO_AUDIO;
  return silenceUrl(`${slug}/lines/${lineId}`, line.duration_ms);
}

function bookFileUrl(slug: Slug, file: string): string {
  const chapter = books.get(slug)?.script.chapters.find((c) => c.audio_file === file);
  if (!chapter || chapter.duration_ms <= 0) return NO_AUDIO;
  return silenceUrl(`${slug}/${file}`, chapter.duration_ms);
}

export function createMockApi(): StudioApi {
  return {
    isMock: true,

    listBooks: (signal?: AbortSignal) =>
      read(
        () =>
          [...books.values()]
            .map((b) => clone(b.script.book))
            .sort((a, c) => (a.updated_at < c.updated_at ? 1 : -1)),
        signal,
      ),

    createBook: (body: NewBookRequest) =>
      delay(newBookFrom(body.title, body.author, body.text, body.book_type, body.casting_mode)),

    uploadBook: (form: FormData) => {
      const file = form.get("file");
      const name = file instanceof File ? file.name.replace(/\.[^.]+$/, "") : "Uploaded book";
      const title = String(form.get("title") || name);
      const author = String(form.get("author") || "Unknown");
      const type = String(form.get("book_type") || "auto");
      // As the studio's upload route: blank means the settings' default.
      const raw = String(form.get("casting_mode") || "");
      const mode = isCastingMode(raw) ? raw : undefined;
      if (raw && !mode) {
        return Promise.reject(
          fail(
            422,
            `unknown casting mode '${raw}'; must be one of ${CASTING_MODES.join(", ")}`,
            "/books/upload",
          ),
        );
      }
      const text =
        "The uploaded file is parsed by the studio service. " +
        "In mock mode its first paragraphs stand in for the book.\n\n" +
        "A second paragraph, so the fixture has more than one chapter.\n\n" +
        "A third, for the same reason.";
      return delay(newBookFrom(title, author, text, type, mode), 320);
    },

    getBook: (slug: Slug, signal?: AbortSignal) =>
      read(() => clone(book(slug).script.book), signal),

    deleteBook: (slug: Slug) => {
      books.delete(slug);
      return delay(undefined as void);
    },

    getScript: (slug: Slug, signal?: AbortSignal) => read(() => clone(book(slug).script), signal),

    patchLine: (slug: Slug, lineId: string, patch: LinePatch) => {
      const b = book(slug);
      const line = b.script.lines.find((l) => l.id === lineId);
      if (!line)
        return Promise.reject(fail(404, `No line "${lineId}"`, `/books/${slug}/lines/${lineId}`));
      let touched = false; // touched = the engine hears something different
      if ("instruction" in patch) {
        line.render.instruction = patch.instruction ?? null;
        line.render.direction_source = patch.instruction ? "manual" : "none";
        touched = true;
      }
      if ("spoken_text" in patch) {
        line.render.spoken_text = patch.spoken_text ?? null;
        line.render.direction_source = "manual";
        touched = true;
      }
      if (patch.engine) {
        line.render.engine = patch.engine;
        const eng = engineById(patch.engine);
        if (eng && eng.voices.length === 0) line.render.voice_id = null;
        else if (eng && !eng.voices.some((v) => v.id === line.render.voice_id)) {
          line.render.voice_id = eng.voices[0]?.id ?? null;
        }
        touched = true;
      }
      if ("voice_id" in patch) {
        line.render.voice_id = patch.voice_id ?? null;
        touched = true;
      }
      if (patch.speed !== undefined) {
        line.render.speed = patch.speed;
        touched = true;
      }
      if (patch.text !== undefined) {
        line.text = patch.text;
        line.render.spoken_text = null; // editing text clears spoken_text
        touched = true;
      }
      if (patch.speaker !== undefined && patch.speaker !== line.speaker) {
        const entry = castFor(b, patch.speaker);
        line.speaker = patch.speaker;
        line.render.engine = entry.engine;
        line.render.voice_id = entry.voice_id;
        line.render.voice_design = entry.voice_design;
        line.attribution = { basis: "manual", confidence: 1 };
        touched = true;
      }
      // flag and note are QC bookkeeping: no engine ever hears them, so they
      // must not cost a re-render.
      if (patch.flag !== undefined) line.flag = patch.flag;
      if (patch.note !== undefined) line.note = patch.note;

      if (touched) {
        markStale(line, `${line.render.instruction ?? ""}|${line.render.spoken_text ?? ""}`);
        saveVersion(slug, "line edit");
      }
      recompute(slug);
      return delay({ line: clone(line), version: b.script.version });
    },

    patchChapter: (slug: Slug, index: number, patch: ChapterPatch) => {
      const b = book(slug);
      const ch = b.script.chapters.find((c) => c.index === index);
      if (!ch)
        return Promise.reject(fail(404, `No chapter ${index}`, `/books/${slug}/chapters/${index}`));
      // v1.2: qc_status lives in the qc.json sidecar, so unlike a title or a
      // kind it saves no version and changes no cache key.
      const sidecarOnly =
        patch.qc_status !== undefined &&
        patch.title === undefined &&
        patch.kind === undefined &&
        patch.include_in_audio === undefined;
      if (patch.title !== undefined) ch.title = patch.title;
      if (patch.kind !== undefined) ch.kind = patch.kind;
      if (patch.include_in_audio !== undefined) ch.include_in_audio = patch.include_in_audio;
      if (patch.qc_status !== undefined) ch.qc_status = patch.qc_status;
      if (!sidecarOnly) saveVersion(slug, `chapter ${index} settings`);
      recompute(slug);
      return delay(clone(ch));
    },

    renderLine: (slug: Slug, lineId: string) => {
      const b = book(slug);
      const line = b.script.lines.find((l) => l.id === lineId);
      if (!line)
        return Promise.reject(fail(404, `No line "${lineId}"`, `/books/${slug}/lines/${lineId}`));
      const dur = speakMs(line.text);
      line.audio_state = "cached";
      line.duration_ms = dur;
      line.audio = {
        start_ms: line.audio?.start_ms ?? 0,
        end_ms: (line.audio?.start_ms ?? 0) + dur,
        sample_rate: 24000,
        cache_key: hashKey(`${line.id}|${line.text}|${line.render.instruction ?? ""}`),
      };
      // The line is cached but the chapter WAV was not re-stitched: this is the
      // state the "Rebuild N chapters" action exists for.
      b.dirty.add(line.chapter_index);
      recompute(slug);
      return delay({ line: clone(line), audio_url: lineAudioUrl(slug, lineId) }, 900);
    },

    lineAudioUrl,
    bookFileUrl,

    getCast: (slug: Slug, signal?: AbortSignal) => read(() => castSummary(slug), signal),

    patchCast: (slug: Slug, entries: CastEntry[]) => {
      const b = book(slug);
      for (const patch of entries) {
        const entry = b.script.cast.find((c) => c.character === patch.character);
        if (!entry) continue;
        Object.assign(entry, patch);
        const seedEntry = b.seed.cast.find((c) => c.character === patch.character);
        if (seedEntry) Object.assign(seedEntry, patch);
        for (const l of b.script.lines) {
          if (l.speaker !== patch.character) continue;
          l.render.engine = entry.engine;
          l.render.voice_id = entry.voice_id;
          l.render.voice_design = entry.voice_design;
          markStale(l, `${entry.engine}|${entry.voice_id ?? ""}`);
        }
      }
      saveVersion(slug, "cast change");
      recompute(slug);
      return delay(clone(b.script));
    },

    mergeCast: (slug: Slug, from: string, into: string) => {
      const b = book(slug);
      if (from === into) return delay(clone(b.script));
      const target = castFor(b, into);
      for (const l of b.script.lines) {
        if (l.speaker !== from) continue;
        l.speaker = into;
        l.render.engine = target.engine;
        l.render.voice_id = target.voice_id;
        l.render.voice_design = target.voice_design;
        l.attribution = { basis: "manual", confidence: 1 };
        markStale(l, `${target.engine}|${target.voice_id ?? ""}`);
      }
      b.script.cast = b.script.cast.filter((c) => c.character !== from);
      b.seed.cast = b.seed.cast.filter((c) => c.character !== from);
      saveVersion(slug, `merged ${from} into ${into}`);
      recompute(slug);
      return delay(clone(b.script));
    },

    getLexicon: (slug: Slug, signal?: AbortSignal) => read(() => lexiconOf(slug), signal),

    putLexicon: (slug: Slug, entries: Record<string, string>) => {
      const b = book(slug);
      const changed = new Set<string>();
      for (const term of new Set([...Object.keys(entries), ...Object.keys(b.lexicon)])) {
        if (entries[term] !== b.lexicon[term]) changed.add(term);
      }
      b.lexicon = { ...entries };
      // A pronunciation change is a new cache key on every line that says it.
      for (const term of changed) {
        const re = new RegExp(`\\b${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i");
        for (const l of b.script.lines) {
          if (re.test(l.text)) markStale(l, `lex:${term}:${entries[term] ?? ""}`);
        }
      }
      saveVersion(slug, "lexicon change");
      recompute(slug);
      return delay(lexiconOf(slug));
    },

    listVersions: (slug: Slug, signal?: AbortSignal) =>
      read(() => clone(book(slug).versions), signal),

    restoreVersion: (slug: Slug, version: number) => {
      const b = book(slug);
      saveVersion(slug, `restored v${version}`);
      recompute(slug);
      return delay(clone(b.script));
    },

    render: (slug: Slug, body: RenderRequest) => {
      const { rows, total } = chapterRows(slug, body.scope === "stale");
      const job = makeJob(
        "render",
        slug,
        total,
        rows,
        { ...body, cache_bias: body.scope === "stale" ? 0.94 : 0.4 },
        () => applyRender(slug, body.scope),
      );
      return delay(job);
    },

    direct: (slug: Slug, body: DirectRequest) => {
      const b = book(slug);
      // v1.1: refuse rather than burn a pass that can never be applied.
      if (b.script.book.directable.lines === 0) {
        return Promise.reject(
          fail(
            409,
            "No line in this book is routed to an engine that takes a written note " +
              `(engines in use: ${Object.keys(b.script.book.engines).join(", ") || "none"}). ` +
              "Recast some lines onto an instruction-capable engine first.",
          ),
        );
      }
      const sceneCount = b.script.chapters.reduce((a, c) => a + c.scenes.length, 0);
      const rows = b.script.chapters.map<JobChapter>((c) => ({
        index: c.index,
        title: c.title,
        done: 0,
        total: c.scenes.length,
        status: "queued",
        seconds: 0,
        engines: { [settings.director.model]: c.scenes.length },
      }));
      // target_engine defaults to the instruction-capable engine most lines use.
      const counts = new Map<string, number>();
      for (const l of b.script.lines) {
        if (!takesDirection(l.render.engine)) continue;
        counts.set(l.render.engine, (counts.get(l.render.engine) ?? 0) + 1);
      }
      const target =
        body.target_engine ??
        [...counts.entries()].sort((x, y) => y[1] - x[1])[0]?.[0] ??
        settings.expressive_engine;
      const job = makeJob("direct", slug, sceneCount, rows, { ...body, cache_bias: 0 }, () => {
        const pass = b.passes[0] ? clone(b.passes[0]) : buildPass(slug);
        pass.id = `pass_${Math.random().toString(16).slice(2, 9)}`;
        pass.created_at = nowIso();
        pass.finished_at = nowIso();
        pass.applied_at = null;
        pass.target_engine = target;
        for (const s of pass.scenes) s.decision = "pending";
        b.passes.unshift(pass);
        const js = jobs.get(job.id);
        if (js) js.job.result = { pass_id: pass.id, counts: pass.counts };
        b.script.book.status = "idle";
      });
      return delay(job);
    },

    exportBook: (slug: Slug, body: ExportRequest) => {
      const b = book(slug);
      if (b.script.chapters.some((c) => c.include_in_audio && !c.fresh)) {
        return Promise.reject(
          fail(
            409,
            "Every chapter must be fresh before export. Render the stale lines, or rebuild the " +
              "chapters whose master audio is older than their lines.",
          ),
        );
      }
      const included = b.script.chapters.filter((c) => c.include_in_audio);
      const rows = included.map<JobChapter>((c) => ({
        index: c.index,
        title: c.title,
        done: 0,
        total: 1,
        status: "queued",
        seconds: 0,
        engines: {},
      }));
      const format = body.format ?? settings.export.format;
      const job = makeJob("export", slug, rows.length, rows, { ...body, cache_bias: 0 }, () => {
        const js = jobs.get(job.id);
        if (format === "acx") {
          // v1.1: one 192 kbps CBR MP3 per included chapter, plus its measurements.
          const files = included.map((c) => {
            const file = `${slug}-${String(c.index).padStart(3, "0")}.mp3`;
            return {
              file,
              url: `/studio/books/${slug}/exports/${file}`,
              bytes: Math.max(120_000, Math.round((c.duration_ms / 1000) * 24_000)),
              chapter: c.index,
              duration_ms: c.duration_ms,
              measured: {
                integrated_lufs: Number((-19 - (c.index % 3) * 0.2).toFixed(1)),
                true_peak_dbtp: Number((-3.6 - (c.index % 2) * 0.2).toFixed(1)),
              },
              checks: { peak_ok: true, loudness_ok: true, duration_ok: true },
            };
          });
          for (const f of files) {
            b.exports.unshift({
              file: f.file,
              url: f.url,
              bytes: f.bytes,
              created_at: nowIso(),
              format: "acx",
              loudness: "acx",
              sample_rate: 44100,
              chapter: f.chapter,
              duration_ms: f.duration_ms,
              bitrate: body.bitrate ?? ACX_BITRATE,
              measured: f.measured,
              checks: f.checks,
              compliance: "pass",
            });
          }
          if (js) js.job.result = { files, compliance: "pass", format: "acx" };
        } else {
          const file = `${slug}.${format}`;
          const entry: ExportFile = {
            file,
            url: `/studio/books/${slug}/exports/${file}`,
            bytes: 44_000_000,
            created_at: nowIso(),
            format,
            loudness: body.loudness ?? settings.export.loudness,
            sample_rate: body.sample_rate ?? settings.export.sample_rate,
            ...NOT_ACX,
          };
          b.exports.unshift(entry);
          if (js)
            js.job.result = {
              file,
              url: entry.url,
              bytes: entry.bytes,
              format: entry.format,
              loudness: entry.loudness,
            };
        }
        b.script.book.status = "idle";
      });
      return delay(job);
    },

    listExports: (slug: Slug, signal?: AbortSignal) =>
      read(() => clone(book(slug).exports), signal),

    listPasses: (slug: Slug, signal?: AbortSignal) =>
      read(
        () =>
          book(slug).passes.map((p) => {
            const { scenes: _s, ...rest } = p;
            return clone(rest) as PassSummary;
          }),
        signal,
      ),

    getPass: (slug: Slug, passId: string, signal?: AbortSignal) => {
      const p = book(slug).passes.find((x) => x.id === passId);
      if (!p)
        return Promise.reject(fail(404, `No pass "${passId}"`, `/books/${slug}/passes/${passId}`));
      return read(() => clone(p), signal);
    },

    decide: (slug: Slug, passId: string, body: DecideRequest) => {
      const p = book(slug).passes.find((x) => x.id === passId);
      if (!p)
        return Promise.reject(fail(404, `No pass "${passId}"`, `/books/${slug}/passes/${passId}`));
      for (const d of body.decisions) {
        const scene = p.scenes.find((s) => s.scene_id === d.scene_id);
        if (!scene) continue;
        scene.decision = d.decision;
        scene.final_instruction =
          d.decision === "rejected" ? null : (d.instruction ?? scene.instruction);
      }
      return delay(clone(p));
    },

    applyPass: (slug: Slug, passId: string) => {
      const b = book(slug);
      const p = b.passes.find((x) => x.id === passId);
      if (!p)
        return Promise.reject(fail(404, `No pass "${passId}"`, `/books/${slug}/passes/${passId}`));
      for (const scene of p.scenes) {
        if (scene.decision === "pending" || scene.decision === "rejected") continue;
        const sc = b.script.chapters.flatMap((c) => c.scenes).find((s) => s.id === scene.scene_id);
        if (!sc) continue;
        const note = scene.final_instruction ?? scene.instruction;
        for (const id of sc.line_ids) {
          const line = b.script.lines.find((l) => l.id === id);
          if (!line) continue;
          // v1.1: intent (and, for engines that take no note, pace → speed) is
          // written regardless of engine, so a pass is audible on Kokoro too.
          line.intent = {
            emotion: scene.emotion,
            intensity: scene.scalar,
            pace: scene.pace ?? line.intent.pace,
          };
          if (!takesDirection(line.render.engine)) {
            if (scene.pace) {
              const next = Math.min(1.3, Math.max(0.7, Number((1 * scene.pace).toFixed(2))));
              if (next !== line.render.speed) {
                line.render.speed = next;
                markStale(line, `speed:${next}`);
              }
            }
            continue;
          }
          if (line.render.direction_source === "manual") continue; // never overwritten
          if (!note || line.render.instruction === note) continue;
          line.render.instruction = note;
          line.render.direction_source = scene.decision === "edited" ? "manual" : "director";
          markStale(line, note);
        }
      }
      p.applied_at = nowIso();
      saveVersion(slug, `applied pass ${p.id}`);
      recompute(slug);
      return delay({ pass: clone(p), script: clone(b.script) });
    },

    retryPass: (slug: Slug, passId: string, scenes: string[]) => {
      const b = book(slug);
      const p = b.passes.find((x) => x.id === passId);
      if (p) {
        for (const s of p.scenes) {
          if (!scenes.includes(s.scene_id)) continue;
          s.attempts += 1;
          s.decision = "pending";
        }
      }
      const rows: JobChapter[] = [
        {
          index: 0,
          title: `${scenes.length} scene${scenes.length === 1 ? "" : "s"}`,
          done: 0,
          total: scenes.length,
          status: "queued",
          seconds: 0,
          engines: { [settings.director.model]: scenes.length },
        },
      ];
      return delay(makeJob("direct", slug, scenes.length, rows, { scenes, cache_bias: 0 }));
    },

    listJobs: (signal?: AbortSignal) =>
      read(
        () =>
          [...jobs.values()]
            .map((js) => summaryOf(js.job))
            .sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
        signal,
      ),

    getJob: (id: string) => {
      const js = jobs.get(id);
      if (!js) return Promise.reject(fail(404, `No job "${id}"`, `/jobs/${id}`));
      if (!js.started && js.job.status === "running") startTicker(js);
      return delay(clone(js.job));
    },

    pauseJob: (id: string) => {
      const js = jobs.get(id);
      if (!js) return Promise.reject(fail(404, `No job "${id}"`, `/jobs/${id}`));
      if (js.job.status === "running") {
        js.job.status = "paused";
        stopTicker(js);
        log(js, "paused between lines");
        emit(js, "progress");
      }
      return delay(clone(js.job));
    },

    resumeJob: (id: string) => {
      const js = jobs.get(id);
      if (!js) return Promise.reject(fail(404, `No job "${id}"`, `/jobs/${id}`));
      if (isResumableJob(js.job.status)) {
        js.job.status = "running";
        js.job.finished_at = null;
        log(js, "resumed from checkpoint");
        startTicker(js);
        emit(js, "progress");
      }
      return delay(clone(js.job));
    },

    cancelJob: (id: string) => {
      const js = jobs.get(id);
      if (!js) return Promise.reject(fail(404, `No job "${id}"`, `/jobs/${id}`));
      stopTicker(js);
      js.job.status = "cancelled";
      js.job.finished_at = nowIso();
      const b = books.get(js.job.book);
      if (b) b.script.book.status = "idle";
      log(js, "cancelled");
      emit(js, "progress");
      emit(js, "done");
      return delay(clone(js.job));
    },

    subscribeJob(id: string, handlers: JobStreamHandlers) {
      const js = jobs.get(id);
      if (!js) {
        // As the real client does for a 404: stop, and let the screen offer Retry.
        window.setTimeout(() => handlers.onGiveUp?.(`No job "${id}"`), 0);
        return () => undefined;
      }
      js.subs.add(handlers);
      if (!js.started && js.job.status === "running") startTicker(js);
      // The contract sends a progress immediately on connect. The backlog of log
      // lines arrives as `log` events, exactly as the service replays them.
      window.setTimeout(() => {
        if (!js.subs.has(handlers)) return;
        handlers.onProgress(frame(js.job));
        for (const entry of js.job.log.slice(-60)) handlers.onLog(entry);
        if (isTerminalJob(js.job.status)) {
          handlers.onDone(clone(js.job));
        }
      }, 0);
      return () => {
        js.subs.delete(handlers);
      };
    },

    getSettings: (signal?: AbortSignal) => read(() => clone(settings), signal),

    patchSettings: (patch: SettingsPatch) => {
      // As store.patch_settings: a given `director` / `export` replaces the
      // stored one wholesale, and a key it leaves out takes the model default.
      settings = {
        ...settings,
        ...(patch.narrator_engine ? { narrator_engine: patch.narrator_engine } : {}),
        ...(patch.expressive_engine ? { expressive_engine: patch.expressive_engine } : {}),
        ...(patch.casting_mode ? { casting_mode: patch.casting_mode } : {}),
        director: patch.director
          ? { provider: "ollama", model: "", ...patch.director }
          : settings.director,
        export: patch.export
          ? { format: "m4b", loudness: "acx", sample_rate: 44100, bitrate: null, ...patch.export }
          : settings.export,
      };
      return delay(clone(settings));
    },

    health: (signal?: AbortSignal) =>
      read<Health>(
        () => ({
          status: "ok",
          gateway: "ok",
          offline: settings.offline,
          fake_director: settings.fake_director,
        }),
        signal,
      ),

    listEngines: (signal?: AbortSignal) => read(() => clone(settings.engines), signal),
    directorModels: (signal?: AbortSignal) => read(() => clone(DIRECTOR_MODELS), signal),
  };
}

/** Ids the UI can deep-link to in mock mode (used by the shelf's demo hints). */
export const MOCK_IDS = {
  liveJob: seededLive,
  doneJob: seededDone,
  interruptedJob: seededInterrupted,
  pass: "pass_9f2ca41",
};
