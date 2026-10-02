import { useEffect, useState } from "react";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { CastingMode, DirectorModels, EngineOption, Settings } from "../api/types";
import { CASTING_MODES, castingModeLabel } from "../domain";
import { ErrorBox, Spinner, useAsync } from "../ui/bits";
import { fmtBytes } from "../ui/format";
import { TopBar } from "../ui/TopBar";

const FREE_TEXT = "__other__";

export function SettingsScreen() {
  const api = useApi();
  const load = useAsync(
    async (signal) => {
      const [settings, engines, models] = await Promise.all([
        api.getSettings(signal),
        api.listEngines(signal).catch(() => [] as EngineOption[]),
        api.directorModels(signal).catch((e: unknown): DirectorModels => ({
          provider: "ollama",
          models: [],
          default: "",
          error: errorMessage(e),
        })),
      ]);
      return { settings, engines, models };
    },
    [api],
  );

  const [draft, setDraft] = useState<Settings | null>(null);
  const [customModel, setCustomModel] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!load.data) return;
    setDraft(load.data.settings);
    const known = load.data.models.models.some(
      (m) => m.name === load.data!.settings.director.model,
    );
    setCustomModel(!known && load.data.models.models.length > 0);
  }, [load.data]);

  if (load.loading || !draft) {
    return (
      <>
        <TopBar crumbs={<span>Settings</span>} />
        <div className="page">
          {load.error ? <ErrorBox error={load.error} onRetry={load.reload} /> : <Spinner />}
        </div>
      </>
    );
  }

  const engines: EngineOption[] = load.data?.engines ?? [];
  const models = load.data?.models;

  function update(patch: Partial<Settings>) {
    setDraft((d) => (d ? { ...d, ...patch } : d));
    setSaved(false);
  }

  async function save() {
    if (!draft) return;
    setSaving(true);
    setError(null);
    try {
      const next = await api.patchSettings({
        narrator_engine: draft.narrator_engine,
        expressive_engine: draft.expressive_engine,
        casting_mode: draft.casting_mode,
        director: draft.director,
        export: draft.export,
      });
      setDraft(next);
      setSaved(true);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  const expressive = engines.find((e) => e.id === draft.expressive_engine);

  return (
    <>
      {/* The offline / fake_director badges used to live here. They now sit in
          the top bar's health pill on every screen, because the screen where you
          need to know the audio is fake is the desk, not this one. The facts
          themselves stay below under Deployment. */}
      <TopBar crumbs={<span>Settings</span>}>
        <button className="btn signal" disabled={saving} onClick={() => void save()}>
          {saving ? "Saving…" : saved ? "Saved" : "Save settings"}
        </button>
      </TopBar>

      <div className="page">
        <h1 className="h1" style={{ marginBottom: 4 }}>
          Settings
        </h1>
        <p className="soft" style={{ marginBottom: 18, fontSize: 14 }}>
          Defaults for new books and for the director. Existing books keep what they were cast with.
        </p>

        {error && <ErrorBox error={error} />}

        <div className="form">
          <section className="col">
            <span className="eyebrow">Engines</span>
            <div className="formrow">
              <div className="field">
                <label htmlFor="st-narrator">Narrator engine</label>
                <select
                  id="st-narrator"
                  value={draft.narrator_engine}
                  onChange={(e) => update({ narrator_engine: e.target.value })}
                >
                  {!engines.some((e) => e.id === draft.narrator_engine) && (
                    <option value={draft.narrator_engine}>
                      {draft.narrator_engine} (not in this deployment's engine list)
                    </option>
                  )}
                  {engines.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.id} — {e.license}
                      {e.available ? "" : " (not running)"}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="st-expressive">Expressive engine</label>
                <select
                  id="st-expressive"
                  value={draft.expressive_engine}
                  onChange={(e) => update({ expressive_engine: e.target.value })}
                >
                  {!engines.some((e) => e.id === draft.expressive_engine) && (
                    <option value={draft.expressive_engine}>
                      {draft.expressive_engine} (not in this deployment's engine list)
                    </option>
                  )}
                  {engines.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.id}
                      {e.instruction ? " · takes direction" : " · no direction"}
                      {e.available ? "" : " (not running)"}
                    </option>
                  ))}
                </select>
                {expressive && !expressive.available && (
                  <span className="hint">
                    Not running right now. Its capabilities above come from the studio's own engine
                    list, so they stay accurate while the container is down.
                  </span>
                )}
                {expressive?.non_commercial && (
                  <span className="hint" style={{ color: "var(--risk)" }}>
                    {expressive.license}. Anything rendered on it is personal use only, including
                    self-hosted output.
                  </span>
                )}
                {expressive && !expressive.instruction && (
                  <span className="hint">
                    This engine does not take a written note, so direction will have no effect.
                  </span>
                )}
              </div>
            </div>
            <div className="field">
              <label htmlFor="st-casting">Casting mode</label>
              <select
                id="st-casting"
                value={draft.casting_mode}
                onChange={(e) => update({ casting_mode: e.target.value as CastingMode })}
              >
                {CASTING_MODES.map((m) => (
                  <option key={m} value={m}>
                    {castingModeLabel(m, "id")}
                  </option>
                ))}
              </select>
            </div>
          </section>

          <section className="col">
            <span className="eyebrow">Director</span>
            {models?.error && (
              <div className="notice risk">
                Ollama is unreachable: <span className="mono">{models.error}</span>. The model below
                is kept, but a pass will fail until it answers.
              </div>
            )}
            <div className="formrow">
              <div className="field">
                <label htmlFor="st-provider">Provider</label>
                <select
                  id="st-provider"
                  value={draft.director.provider}
                  onChange={(e) =>
                    update({
                      director: {
                        ...draft.director,
                        provider: e.target.value as "ollama" | "gemini",
                      },
                    })
                  }
                >
                  <option value="ollama">ollama</option>
                  <option value="gemini">gemini</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="st-model">Model</label>
                <select
                  id="st-model"
                  value={customModel ? FREE_TEXT : draft.director.model}
                  onChange={(e) => {
                    if (e.target.value === FREE_TEXT) {
                      setCustomModel(true);
                      return;
                    }
                    setCustomModel(false);
                    update({ director: { ...draft.director, model: e.target.value } });
                  }}
                >
                  {(models?.models ?? []).map((m) => (
                    <option key={m.name} value={m.name}>
                      {m.name}
                      {m.name === models?.default ? "  (default)" : ""} — {fmtBytes(m.size_bytes)}
                    </option>
                  ))}
                  {!models?.models.length && (
                    <option value={draft.director.model}>{draft.director.model}</option>
                  )}
                  <option value={FREE_TEXT}>other — type a name…</option>
                </select>
                {customModel && (
                  <input
                    type="text"
                    value={draft.director.model}
                    placeholder="qwen3:30b"
                    onChange={(e) =>
                      update({ director: { ...draft.director, model: e.target.value } })
                    }
                  />
                )}
                <span className="hint">
                  A model Ollama has not pulled is a visible choice here, not a silent 404.
                </span>
              </div>
            </div>
          </section>

          <section className="col">
            <span className="eyebrow">Export defaults</span>
            <div className="formrow">
              <div className="field">
                <label htmlFor="st-format">Format</label>
                <select
                  id="st-format"
                  value={draft.export.format}
                  onChange={(e) =>
                    update({
                      export: {
                        ...draft.export,
                        format: e.target.value as Settings["export"]["format"],
                      },
                    })
                  }
                >
                  {!["m4b", "mp3", "wav"].includes(draft.export.format) && (
                    <option value={draft.export.format}>{draft.export.format}</option>
                  )}
                  <option value="m4b">m4b — chaptered audiobook</option>
                  <option value="mp3">mp3</option>
                  <option value="wav">wav</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="st-loudness">Loudness</label>
                <select
                  id="st-loudness"
                  value={draft.export.loudness}
                  onChange={(e) =>
                    update({
                      export: {
                        ...draft.export,
                        loudness: e.target.value as Settings["export"]["loudness"],
                      },
                    })
                  }
                >
                  <option value="acx">acx — ffmpeg loudnorm</option>
                  <option value="none">none</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="st-rate">Sample rate</label>
                <select
                  id="st-rate"
                  value={draft.export.sample_rate}
                  onChange={(e) =>
                    update({ export: { ...draft.export, sample_rate: Number(e.target.value) } })
                  }
                >
                  {/* A stored rate the list does not have used to display as
                      44100 while the draft still held (and saved) the old one. */}
                  {![44100, 48000, 24000].includes(draft.export.sample_rate) && (
                    <option value={draft.export.sample_rate}>
                      {draft.export.sample_rate} (from settings)
                    </option>
                  )}
                  <option value={44100}>44100</option>
                  <option value={48000}>48000</option>
                  <option value={24000}>24000</option>
                </select>
              </div>
            </div>
            {!draft.ffmpeg && (
              <div className="notice risk">
                ffmpeg is not available in this deployment, so mastering and .m4b export will fail.
              </div>
            )}
          </section>

          <section className="col">
            <span className="eyebrow">Deployment</span>
            <div className="stats">
              <span>gateway {draft.gateway_url}</span>
              <span>offline {String(draft.offline)}</span>
              <span>fake_director {String(draft.fake_director)}</span>
              <span>ffmpeg {String(draft.ffmpeg)}</span>
            </div>
            <span className="hint">
              These are read from the studio service's environment and cannot be changed here.
            </span>
          </section>
        </div>
      </div>
    </>
  );
}
