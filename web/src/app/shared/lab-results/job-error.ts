/** What a trader reads when a lab job stopped on our side. */
export const FAILED_RUN_TEXT =
  'The test stopped because of a problem on our side. Try again, or run it with the Quick tests.';

/** A Python exception as the job runner stores it: "TypeError: cannot pickle ...". */
const CRASH = /^(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Interrupt|Exit|Warning)\b:?/;

/**
 * A failed job's error in plain words. A crash (a raw exception) reads as
 * one plain sentence with the technical text kept for a Details disclosure.
 * An error the server wrote for people ("Enter at least one ticker") shows
 * as it is.
 */
export function jobErrorText(error: string): { text: string; details: string | null } {
  const raw = error.trim();
  if (!raw || CRASH.test(raw) || raw.includes('Traceback')) {
    return { text: FAILED_RUN_TEXT, details: raw || null };
  }
  return { text: raw, details: null };
}
