import { useState } from "react";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { ExportFile, Script, VersionEntry } from "../api/types";
import { ErrorBox, Spinner, useAsync } from "../ui/bits";
import { fmtAgo, fmtBytes, fmtDuration } from "../ui/format";

/**
 * The two endpoint groups that existed, were tested, and had no caller: the
 * exports library and the version history. Both open over the script from the
 * rail, because that is where you are when you want them.
 */
export function ExportsPanel({ slug, onClose }: { slug: string; onClose: () => void }) {
  const api = useApi();
  const load = useAsync((signal) => api.listExports(slug, signal), [api, slug]);

  return (
    <section className="deskpanel" aria-label="Exports">
      <header>
        <span className="eyebrow">Exports</span>
        <span className="spacer" />
        <button className="btn sm ghost" onClick={onClose}>
          Close
        </button>
      </header>
      <div className="body">
        {load.loading && <Spinner label="reading the exports folder" />}
        {load.error && <ErrorBox error={load.error} onRetry={load.reload} />}
        {load.data?.length === 0 && (
          <p className="hint" style={{ margin: 0 }}>
            Nothing exported yet. Export from the shelf once every chapter is fresh.
          </p>
        )}
        {load.data?.map((f) => (
          <ExportRow key={f.url} file={f} />
        ))}
      </div>
    </section>
  );
}

function ExportRow({ file }: { file: ExportFile }) {
  const checks = file.checks;
  const failed =
    file.compliance === "fail" ||
    (checks && (!checks.peak_ok || !checks.loudness_ok || !checks.duration_ok));
  return (
    <div className="panelrow">
      <span className="grow">{file.file}</span>
      <span className="faint">{file.format}</span>
      {file.chapter != null && <span className="faint">ch {file.chapter + 1}</span>}
      <span className="faint">{fmtBytes(file.bytes)}</span>
      {file.duration_ms != null && <span className="faint">{fmtDuration(file.duration_ms)}</span>}
      <span className="faint">loudness {file.loudness}</span>
      {file.measured && (
        <span className="faint" title="Integrated loudness / true peak, measured after mastering">
          {file.measured.integrated_lufs.toFixed(1)} LUFS ·{" "}
          {file.measured.true_peak_dbtp.toFixed(1)} dBTP
        </span>
      )}
      {(file.compliance || checks) && (
        <span
          className={`chip ${failed ? "unknown" : ""}`}
          title={
            checks
              ? `peak ${checks.peak_ok ? "ok" : "fail"} · loudness ${
                  checks.loudness_ok ? "ok" : "fail"
                } · duration ${checks.duration_ok ? "ok" : "fail"}`
              : undefined
          }
        >
          {failed ? "compliance fail" : "compliance pass"}
        </span>
      )}
      <span className="faint">{fmtAgo(file.created_at)}</span>
      <a className="btn sm" href={file.url} download>
        Download
      </a>
    </div>
  );
}

export function VersionsPanel({
  slug,
  currentVersion,
  onRestored,
  onClose,
}: {
  slug: string;
  currentVersion: number;
  onRestored: (script: Script) => void;
  onClose: () => void;
}) {
  const api = useApi();
  // Read again whenever the desk's version moves (an edit or a restore saved
  // one), so the list's newest entry and "current" agree.
  const load = useAsync((signal) => api.listVersions(slug, signal), [api, slug, currentVersion]);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function restore(v: VersionEntry) {
    setBusy(v.version);
    setError(null);
    try {
      const script = await api.restoreVersion(slug, v.version);
      onRestored(script);
      load.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="deskpanel" aria-label="Versions">
      <header>
        <span className="eyebrow">Versions</span>
        <span className="mono faint" style={{ fontSize: 10.5 }}>
          current v{currentVersion}
        </span>
        <span className="spacer" />
        <button className="btn sm ghost" onClick={onClose}>
          Close
        </button>
      </header>
      <div className="body">
        {error && <ErrorBox error={error} />}
        {load.loading && <Spinner label="reading the version history" />}
        {load.error && <ErrorBox error={load.error} onRetry={load.reload} />}
        {load.data?.length === 0 && (
          <p className="hint" style={{ margin: 0 }}>
            No saved versions yet.
          </p>
        )}
        {load.data?.map((v) => (
          <div className="panelrow" key={v.version}>
            <span style={{ width: 54 }}>v{v.version}</span>
            <span className="grow">{v.note}</span>
            <span className="faint">{fmtAgo(v.saved_at)}</span>
            <button
              className="btn sm"
              disabled={busy !== null || v.version === currentVersion}
              title={
                v.version === currentVersion
                  ? "This is the working script"
                  : "Copy this version back onto the working script (saved as a new version)"
              }
              onClick={() => void restore(v)}
            >
              {busy === v.version ? "Restoring…" : "Restore"}
            </button>
          </div>
        ))}
        <p className="hint" style={{ marginTop: 8 }}>
          Restoring never deletes anything: the old script is copied forward as a new version.
        </p>
      </div>
    </section>
  );
}
