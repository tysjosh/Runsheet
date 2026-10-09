/**
 * Read a throttled (429) sign-in / password-reset rejection (staging finding F5).
 *
 * The SuperTokens web SDK rejects any non-2xx answer with the raw fetch
 * `Response`, so a 429 would otherwise read as "Invalid credentials". The
 * backend's 429 body carries `details.retry_after_seconds`; the `Retry-After`
 * header is the fallback (exposed to the UI via CORS `expose_headers`).
 * Duck-typed rather than `instanceof Response`, which jsdom may not provide.
 */

interface ResponseLike {
  status?: unknown;
  headers?: { get?: (name: string) => string | null };
  clone?: () => { json?: () => Promise<unknown> };
  json?: () => Promise<unknown>;
}

export function formatThrottleMessage(seconds?: number): string {
  return seconds && seconds > 0
    ? `Too many attempts, try again in ${Math.ceil(seconds)} seconds`
    : "Too many attempts, try again later";
}

function positive(value: unknown): number | undefined {
  const n = typeof value === "string" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? n : undefined;
}

async function retryFromBody(res: ResponseLike): Promise<number | undefined> {
  try {
    const source = typeof res.clone === "function" ? res.clone() : res;
    if (typeof source.json !== "function") return undefined;
    const body = (await source.json()) as {
      details?: { retry_after_seconds?: unknown };
    } | null;
    return positive(body?.details?.retry_after_seconds);
  } catch {
    return undefined;
  }
}

/** The wait message when `err` is a 429 response, otherwise `null`. */
export async function throttledMessage(err: unknown): Promise<string | null> {
  if (typeof err !== "object" || err === null) return null;
  const res = err as ResponseLike;
  if (res.status !== 429) return null;
  const seconds =
    (await retryFromBody(res)) ??
    positive(res.headers?.get?.("Retry-After") ?? undefined);
  return formatThrottleMessage(seconds);
}
