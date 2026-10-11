/** What a trader reads when a lab job stopped on our side. */
export const FAILED_RUN_TEXT =
  'The test stopped because of a problem on our side. Try again, or run it with the Quick tests.';

/** A Python exception as older job rows store it: "TypeError: cannot pickle ...". */
const CRASH = /^(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Interrupt|Exit|Warning)\b:?/;

/**
 * A failed job's error in plain words. A raw exception reads as one plain
 * sentence. An error the server wrote for people ("Enter at least one
 * ticker") shows as it is. `detail` is the raw error the server sends to
 * admins only, kept for a Details disclosure.
 */
export function jobErrorText(
  error: string,
  detail: string | null = null,
): { text: string; details: string | null } {
  const raw = error.trim();
  const details = detail?.trim() || null;
  if (!raw || CRASH.test(raw) || raw.includes('Traceback')) {
    return { text: FAILED_RUN_TEXT, details };
  }
  return { text: raw, details };
}
