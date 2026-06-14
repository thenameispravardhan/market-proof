// Thin fetch wrapper used by all useApi hooks.
// Throws ApiClientError (with .status) on non-2xx responses.

export class ApiClientError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown
  ) {
    super(message);
    this.name = "ApiClientError";
  }
}

/** Read the response body exactly once. We always read it as text
 *  first (cheap, never throws "body stream already read"), and then
 *  attempt to parse it as JSON. A 4xx/5xx can carry a non-JSON body
 *  — e.g. FastAPI's default 500 is `text/plain: "Internal Server
 *  Error"` — and calling `res.json()` on that throws *and* consumes
 *  the body, so the fallback `res.text()` then explodes with
 *  "Failed to execute 'text' on 'Response': body stream already
 *  read". One read, one parse, done. */
async function readBody(res: Response): Promise<{ raw: string; parsed: unknown }> {
  const raw = await res.text();
  if (raw.length === 0) return { raw, parsed: undefined };
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    try {
      return { raw, parsed: JSON.parse(raw) };
    } catch {
      return { raw, parsed: undefined };
    }
  }
  return { raw, parsed: undefined };
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const { raw, parsed } = await readBody(res);
    // FastAPI's HTTPException serializes as `{ "detail": "..." }`.
    // Other 5xx / generic errors may just be plain text — surface
    // the raw text so the operator sees something useful.
    const detailMsg =
      parsed !== undefined &&
      typeof parsed === "object" &&
      parsed !== null &&
      "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : raw.trim() || `HTTP ${res.status}`;
    throw new ApiClientError(res.status, detailMsg, parsed ?? raw);
  }
  // 204 No Content
  if (res.status === 204) return undefined as unknown as T;
  const { raw, parsed } = await readBody(res);
  if (parsed !== undefined) return parsed as T;
  // Some 2xx responses (rare in this app) carry non-JSON bodies.
  // Fall through to the raw text rather than throwing.
  return raw as unknown as T;
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, body: unknown) =>
    request<T>(url, { method: "POST", body: JSON.stringify(body) }),
  put: <T>(url: string, body: unknown) =>
    request<T>(url, { method: "PUT", body: JSON.stringify(body) }),
  delete: <T>(url: string) => request<T>(url, { method: "DELETE" }),
};
