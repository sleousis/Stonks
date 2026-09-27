import { DestroyRef, Injectable, computed, signal } from '@angular/core';

export type CommandGroup = 'Actions' | 'Pages';

/** Something the command palette can run: open a page or do an action. */
export interface PaletteCommand {
  /** Unique; registering the same id again replaces the old command. */
  id: string;
  label: string;
  group: CommandGroup;
  /** Extra words that should find it ("run", "dry"). */
  keywords?: readonly string[];
  /** Shown on the right, e.g. the keyboard shortcut "g d". */
  hint?: string;
  /**
   * Hidden when this returns false (a page or action the user may not use).
   * It may read signals: the palette re-checks when they change.
   */
  visible?: () => boolean;
  run: () => void | Promise<void>;
}

/**
 * Commands available in the palette. The shell registers pages and global
 * actions; a page can add its own while it is open:
 *
 *   inject(CommandRegistry).register(
 *     [{ id: 'lab.new-backtest', label: 'New backtest', group: 'Actions', run: () => ... }],
 *     inject(DestroyRef),   // removed when the page is destroyed
 *   );
 */
@Injectable({ providedIn: 'root' })
export class CommandRegistry {
  private readonly byId = signal<ReadonlyMap<string, PaletteCommand>>(new Map());

  /** Every registered command, hidden ones included. */
  readonly commands = computed(() => [...this.byId().values()]);
  /** The commands this user may run now (reactive to `visible`). */
  readonly available = computed(() => this.commands().filter((c) => c.visible?.() ?? true));

  /** Adds commands; returns a function that removes them (also run on `destroyRef`). */
  register(commands: readonly PaletteCommand[], destroyRef?: DestroyRef): () => void {
    this.byId.update((map) => {
      const next = new Map(map);
      for (const c of commands) next.set(c.id, c);
      return next;
    });
    const remove = () =>
      this.byId.update((map) => {
        const next = new Map(map);
        for (const c of commands) if (next.get(c.id) === c) next.delete(c.id);
        return next;
      });
    destroyRef?.onDestroy(remove);
    return remove;
  }
}

/**
 * How well `query` matches `text`: 0 is no match; higher is better.
 * Prefix beats word start beats substring beats letters in order.
 */
export function matchScore(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const t = text.toLowerCase();
  if (t === q) return 100;
  if (t.startsWith(q)) return 80;
  const index = t.indexOf(q);
  if (index > 0 && /[\s\-_./:]/.test(t[index - 1])) return 60;
  if (index > 0) return 40;
  // Letters in order, starting at a word start ("mv3" finds "momentum-v3",
  // but "sha" does not find "Dashboard").
  const starts = [...t].flatMap((ch, i) =>
    ch === q[0] && (i === 0 || /[\s\-_./:]/.test(t[i - 1])) ? [i] : [],
  );
  if (!starts.length) return 0;
  let at = starts[0];
  for (const ch of q) {
    if (ch === ' ') continue;
    at = t.indexOf(ch, at);
    if (at < 0) return 0;
    at += 1;
  }
  return 10;
}

/** Best score of `query` against a label and its keywords. */
export function commandScore(
  query: string,
  label: string,
  keywords: readonly string[] = [],
): number {
  let best = matchScore(query, label);
  for (const k of keywords) best = Math.max(best, matchScore(query, k) * 0.9);
  return best;
}
