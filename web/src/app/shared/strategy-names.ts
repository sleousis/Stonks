/**
 * Names for strategies that a trader can read (UX-27). Registered ids look
 * like `stocks_on_the_move_3fa9c21b` (a slug of the kind or draft name plus
 * eight hex characters). Pages show the display name and keep the id under
 * "Technical details".
 */

/** A slug and the registry's eight-character hex suffix. */
const GENERATED_ID = /^(.+?)_([0-9a-f]{8})$/;

export interface DisplayNameHints {
  /** The Studio draft's own name, when the strategy came from a draft. */
  draftName?: string | null;
}

/**
 * `stocks_on_the_move_3fa9c21b` -> `Stocks on the move 3fa9`. A draft name
 * wins when known. An id without the generated suffix is shown as it is.
 */
export function strategyDisplayName(id: string, hints: DisplayNameHints = {}): string {
  const draft = hints.draftName?.trim();
  if (draft) return draft;
  const match = GENERATED_ID.exec(id);
  if (!match) return id;
  const words = match[1].replace(/_+/g, ' ').trim();
  return `${words.charAt(0).toUpperCase()}${words.slice(1)} ${match[2].slice(0, 4)}`;
}

/** `stonks.strategies.momentum.MomentumStrategy` or `pkg.mod:Class` -> the class name. */
export function shortClassName(classPath: string): string {
  return classPath.split(/[.:]/).at(-1) || classPath;
}

/**
 * A readable name for the strategy's kind, from its class:
 * `stonks.strategies.momentum.MomentumStrategy` -> `Momentum strategy`,
 * `RSIMeanReversion` -> `RSI mean reversion`.
 */
export function strategyKindName(classPath: string): string {
  const words = shortClassName(classPath)
    .replace(/_/g, ' ')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
    .split(/\s+/);
  return words
    .map((w, i) => {
      if (w.length > 1 && w === w.toUpperCase()) return w;
      return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w.toLowerCase();
    })
    .join(' ');
}
