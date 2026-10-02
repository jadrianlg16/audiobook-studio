import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useApi } from "../api/client";
import { errorMessage } from "../api/http";
import type { CastingMode } from "../api/types";
import { CASTING_MODES, castingModeLabel } from "../domain";
import { ErrorBox, Spinner, useAsync } from "../ui/bits";
import { titleCase } from "../ui/format";
import { TopBar } from "../ui/TopBar";

type Source = "paste" | "upload";
type Voices = "auto" | "single" | "multi";

export function NewBook() {
  const api = useApi();
  const navigate = useNavigate();
  const settings = useAsync((signal) => api.getSettings(signal), [api]);

  const [source, setSource] = useState<Source>("paste");
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [text, setText] = useState("");
  const [bookType, setBookType] = useState("auto");
  const [voices, setVoices] = useState<Voices>("auto");
  const [castingMode, setCastingMode] = useState<CastingMode | "">("");
  const [file, setFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const ready = source === "paste" ? text.trim().length > 0 && title.trim().length > 0 : !!file;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    try {
      let book;
      if (source === "upload" && file) {
        const form = new FormData();
        form.append("file", file);
        if (title.trim()) form.append("title", title.trim());
        if (author.trim()) form.append("author", author.trim());
        form.append("book_type", bookType);
        form.append("voices", voices);
        // Left out, the studio applies its own default, as for a pasted book.
        if (castingMode) form.append("casting_mode", castingMode);
        book = await api.uploadBook(form);
      } else {
        book = await api.createBook({
          title: title.trim(),
          author: author.trim(),
          text,
          book_type: bookType,
          voices,
          ...(castingMode ? { casting_mode: castingMode } : {}),
        });
      }
      navigate(`/books/${book.slug}`);
    } catch (err) {
      setError(errorMessage(err));
      setSaving(false);
    }
  }

  return (
    <>
      <TopBar crumbs={<span>New book</span>} />
      <div className="page">
        <h1 className="h1" style={{ marginBottom: 4 }}>
          New book
        </h1>
        <p className="soft" style={{ marginBottom: 18, fontSize: 14 }}>
          Paste the text or upload a file. Accepted files: <span className="mono">.txt</span>,{" "}
          <span className="mono">.md</span>, <span className="mono">.epub</span> — PDF is not
          supported yet. Nothing is rendered yet.
        </p>

        {settings.loading && <Spinner label="reading settings" />}
        {settings.error && <ErrorBox error={settings.error} onRetry={settings.reload} />}
        {error && <ErrorBox error={error} />}

        <form className="form" onSubmit={submit}>
          <div className="seg" role="tablist" aria-label="Source">
            <button
              type="button"
              role="tab"
              aria-selected={source === "paste"}
              className={source === "paste" ? "on" : ""}
              onClick={() => setSource("paste")}
            >
              Paste text
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={source === "upload"}
              className={source === "upload" ? "on" : ""}
              title="Accepts .txt, .md and .epub. PDF is not supported yet."
              onClick={() => setSource("upload")}
            >
              Upload a file (.txt, .md, .epub)
            </button>
          </div>

          {source === "paste" ? (
            <div className="field">
              <label htmlFor="nb-text">Text</label>
              <textarea
                id="nb-text"
                className="paste"
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Paste the book here. Blank lines separate paragraphs; chapter headings are detected."
              />
              <span className="hint">
                {text.trim() ? `${text.trim().split(/\s+/).length} words` : "empty"}
              </span>
            </div>
          ) : (
            <div className="field">
              <label htmlFor="nb-file">File</label>
              <input
                id="nb-file"
                ref={fileInput}
                type="file"
                accept=".txt,.md,.epub"
                className="mono"
                style={{ fontSize: 12 }}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              <span className="hint">
                .txt, .md or .epub. PDF is not supported yet — convert it to text first, or the
                upload is rejected. EPUB metadata fills the title and author when you leave them
                blank.
              </span>
            </div>
          )}

          <div className="formrow">
            <div className="field">
              <label htmlFor="nb-title">Title</label>
              <input
                id="nb-title"
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={source === "upload" ? "from the file" : "The Cold Lantern"}
              />
            </div>
            <div className="field">
              <label htmlFor="nb-author">Author</label>
              <input
                id="nb-author"
                type="text"
                value={author}
                onChange={(e) => setAuthor(e.target.value)}
                placeholder="Unknown"
              />
            </div>
          </div>

          <div className="formrow">
            <div className="field">
              <label htmlFor="nb-type">Book type</label>
              <select id="nb-type" value={bookType} onChange={(e) => setBookType(e.target.value)}>
                <option value="auto">auto — detect from the text</option>
                {(settings.data?.book_types ?? []).map((t) => (
                  <option key={t} value={t}>
                    {titleCase(t)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="nb-voices">Voices</label>
              <select
                id="nb-voices"
                value={voices}
                onChange={(e) => setVoices(e.target.value as Voices)}
              >
                <option value="auto">auto — multi for fiction</option>
                <option value="single">single — one narrator</option>
                <option value="multi">multi — cast per character</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="nb-casting">Casting mode</label>
              <select
                id="nb-casting"
                value={castingMode}
                onChange={(e) => setCastingMode(e.target.value as CastingMode | "")}
              >
                <option value="">
                  default —{" "}
                  {settings.data ? castingModeLabel(settings.data.casting_mode) : "from settings"}
                </option>
                {CASTING_MODES.map((m) => (
                  <option key={m} value={m}>
                    {castingModeLabel(m)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="row">
            <button className="btn signal" type="submit" disabled={!ready || saving}>
              {saving ? "Building the script…" : "Create book"}
            </button>
            <button className="btn ghost" type="button" onClick={() => navigate("/")}>
              Cancel
            </button>
            <span className="hint">Builds the Book Script. Rendering is a separate step.</span>
          </div>
        </form>
      </div>
    </>
  );
}
