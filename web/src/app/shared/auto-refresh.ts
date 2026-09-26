import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  Injectable,
  type Signal,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  signal,
  untracked,
} from '@angular/core';

/** What `autoRefresh` needs from a resource. `resource()` refs fit. */
export interface Reloadable {
  reload(): unknown;
  isLoading(): boolean;
  hasValue(): boolean;
}

export interface AutoRefreshOptions {
  /** Reload every this many ms while the tab is visible. Default 60 s. */
  everyMs?: number;
  /** Extra signals that trigger a reload when they change (e.g. a tick finished). */
  triggers?: readonly Signal<unknown>[];
}

export interface AutoRefresh {
  /** When every resource last finished loading (epoch ms); null before the first load. */
  readonly updatedAt: Signal<number | null>;
  /** Reload now and restart the timer. */
  refresh(): void;
}

export const AUTO_REFRESH_MS = 60_000;
/** How often the timer checks whether a reload is due (and the "ago" clock ticks). */
const CHECK_MS = 5_000;

/**
 * A page header's freshness, for pages whose resources live in child routes
 * (Orders): provide it on the parent, and every `autoRefresh()` below it
 * reports here.
 *
 *   providers: [RefreshStatus]
 *   <app-updated-ago [at]="status.updatedAt()" />
 */
@Injectable()
export class RefreshStatus {
  private readonly source = signal<Signal<number | null> | null>(null);
  readonly updatedAt = computed(() => this.source()?.() ?? null);

  /** @internal Used by autoRefresh(). */
  attach(updatedAt: Signal<number | null>, destroyRef: DestroyRef): void {
    this.source.set(updatedAt);
    destroyRef.onDestroy(() => {
      if (untracked(this.source) === updatedAt) this.source.set(null);
    });
  }
}

/**
 * Reloads the given resources every minute while the tab is visible, pauses
 * while it is hidden, and reloads at once when the trader comes back to a
 * stale tab. `reload()` keeps the current values on screen, so nothing
 * flashes. Call it in an injection context (a field initialiser).
 *
 *   protected readonly auto = autoRefresh(() => [this.portfolio, this.ticks]);
 *   <app-updated-ago [at]="auto.updatedAt()" />
 */
export function autoRefresh(
  resources: () => readonly Reloadable[],
  options: AutoRefreshOptions = {},
): AutoRefresh {
  const everyMs = options.everyMs ?? AUTO_REFRESH_MS;
  const doc = inject(DOCUMENT);
  const destroyRef = inject(DestroyRef);
  const view = doc.defaultView;
  let lastRun = Date.now();

  const visible = () => doc.visibilityState !== 'hidden';
  const reloadAll = () => {
    lastRun = Date.now();
    for (const r of untracked(resources)) r.reload();
  };
  const check = () => {
    if (visible() && Date.now() - lastRun >= everyMs) reloadAll();
  };

  const timer = view?.setInterval(check, Math.min(CHECK_MS, everyMs));
  const onVisibility = () => check();
  doc.addEventListener('visibilitychange', onVisibility);
  destroyRef.onDestroy(() => {
    if (timer !== undefined) view?.clearInterval(timer);
    doc.removeEventListener('visibilitychange', onVisibility);
  });

  const triggers = options.triggers ?? [];
  if (triggers.length) {
    let first = true;
    effect(() => {
      for (const t of triggers) t();
      if (first) {
        first = false;
        return;
      }
      untracked(reloadAll);
    });
  }

  const updatedAt = linkedSignal<{ busy: boolean; loaded: boolean }, number | null>({
    source: () => {
      const list = resources();
      return { busy: list.some((r) => r.isLoading()), loaded: list.some((r) => r.hasValue()) };
    },
    computation: ({ busy, loaded }, prev) => (!busy && loaded ? Date.now() : (prev?.value ?? null)),
  });

  inject(RefreshStatus, { optional: true })?.attach(updatedAt, destroyRef);

  return { updatedAt, refresh: reloadAll };
}

/** "Updated just now", "Updated 2 min ago", "Updated 3 h ago". */
export function updatedLabel(at: number | null, now: number): string {
  if (at === null) return '';
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 60) return 'Updated just now';
  if (s < 3600) return `Updated ${Math.floor(s / 60)} min ago`;
  return `Updated ${Math.floor(s / 3600)} h ago`;
}

/** The small "Updated 2 min ago" line under a page title. */
@Component({
  selector: 'app-updated-ago',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@if (label(); as l) {
    <span class="updated">{{ l }}</span>
  }`,
  styles: `
    :host {
      display: block;
    }
    .updated {
      display: inline-block;
      margin-top: var(--space-1);
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class UpdatedAgo {
  readonly at = input<number | null>(null);
  private readonly now = signal(Date.now());
  protected readonly label = computed(() => updatedLabel(this.at(), this.now()));

  constructor() {
    const view = inject(DOCUMENT).defaultView;
    const timer = view?.setInterval(() => this.now.set(Date.now()), 30_000);
    inject(DestroyRef).onDestroy(() => {
      if (timer !== undefined) view?.clearInterval(timer);
    });
  }
}
