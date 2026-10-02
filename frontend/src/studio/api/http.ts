/**
 * The one fetch wrapper: the studio client (real.ts) and the lab's gateway
 * client (lab/api.ts) both go through `http()`, so every failed request
 * surfaces the same way — as an `ApiError` that keeps the status, the server's
 * message and the URL, instead of an `Error("HTTP 409")` that lost two of them.
 */

/** A non-2xx answer. `message` is `detail`, so code that shows `e.message` is unchanged. */
export class ApiError extends Error {
  override readonly name = "ApiError";

  constructor(
    /** The HTTP status (409, 422, 503, ...). */
    readonly status: number,
    /** The server's human-readable message: `{"detail": "..."}` on the studio (STUDIO-API.md). */
    readonly detail: string,
    /** The URL that answered. */
    readonly url: string,
  ) {
    super(detail);
  }

  /** Read a failed response's body into an ApiError. Never throws. */
  static async from(res: Response, url: string): Promise<ApiError> {
    let text = "";
    try {
      text = await res.text();
    } catch {
      // a body that cannot be read: fall through to the status line
    }
    return new ApiError(res.status, detailOf(res, text), url);
  }
}

/** Longest plain-text body quoted back verbatim; beyond it, the start of it. */
const MAX_TEXT = 300;

function detailOf(res: Response, text: string): string {
  const statusLine = `HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ""}`;
  const body = text.trim();
  if (!body) return statusLine;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    const detail = parsed?.detail;
    if (typeof detail === "string" && detail) return detail;
    // FastAPI's own 422 (the gateway still sends it): [{loc, msg}, ...].
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) =>
          [Array.isArray(d?.loc) ? d.loc.join(".") : "", d?.msg ?? JSON.stringify(d)]
            .filter(Boolean)
            .join(": "),
        )
        .join("; ");
    }
    if (detail !== undefined) return JSON.stringify(detail);
  } catch {
    // not JSON
  }
  // An HTML error page (nginx's 502) says nothing a person needs twice.
  if ((res.headers.get("content-type") ?? "").includes("html")) return statusLine;
  return body.length > MAX_TEXT ? `${body.slice(0, MAX_TEXT)}…` : body;
}

/**
 * `fetch` that throws `ApiError` on a non-2xx status and parses the JSON body
 * otherwise (`undefined` for a 204 or an empty body). Network failures and
 * aborts reject exactly as `fetch` does.
 */
export async function http<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw await ApiError.from(res, url);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** A JSON request body with its method. */
export function jsonBody(method: "POST" | "PUT" | "PATCH", body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/**
 * What to show a person for a caught error. The one place that decides it —
 * screens call this instead of repeating `e instanceof Error ? e.message :
 * String(e)`. An `ApiError`'s message is the server's `detail`.
 */
export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
