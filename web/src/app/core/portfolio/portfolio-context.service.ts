import { DestroyRef, Injectable, computed, inject, linkedSignal, signal } from '@angular/core';

import { type PortfolioRef, PortfoliosService } from '../../api/portfolios.service';
import { SessionService } from '../auth/session.service';
import { ApiError } from '../http/api-error';

const STORAGE_KEY = 'stonks.portfolio';
/** Waits before retrying a failed list read: 2 s, 4 s, 8 s, ... up to a minute. */
const RETRY_BASE_MS = 2000;
const RETRY_MAX_MS = 60_000;

/**
 * Which portfolio the portfolio, orders, fills, P&L and trade-cost reads
 * show. `PortfolioService`, `OrdersService` and `TcaService` add
 * `portfolio_id` to their queries from here; pages put `selectedId()` in
 * their resource params so they reload when it changes.
 *
 * - No selection (null) means "my default portfolio": nothing is sent.
 * - The list comes from `GET /api/portfolios`. On a server without that
 *   route, `state` is `missing`, the picker stays hidden and reads behave
 *   as before.
 * - The choice is remembered per user on this browser
 *   (`stonks.portfolio.<user_id>`), and dropped if the portfolio is no
 *   longer in the list.
 * - A failed read retries on its own with a growing wait (UX-15), so one
 *   blip at start-up does not hide the LIVE stamp for the session.
 */
@Injectable({ providedIn: 'root' })
export class PortfolioContextService {
  private readonly api = inject(PortfoliosService);
  private readonly session = inject(SessionService);
  private readonly userId = computed(() => this.session.me()?.user_id ?? null);

  private readonly optionsSignal = signal<PortfolioRef[]>([]);
  private readonly stateSignal = signal<'idle' | 'loading' | 'ready' | 'missing' | 'failed'>(
    'idle',
  );
  /** This user's stored choice; only used once the list confirms it still exists. */
  private readonly chosen = linkedSignal(() => readStored(this.userId()));
  private loading: Promise<void> | null = null;
  /** Bumped per read, so a forced re-read wins over one still running. */
  private generation = 0;
  private failures = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  readonly options = this.optionsSignal.asReadonly();
  readonly state = this.stateSignal.asReadonly();
  /**
   * The chosen portfolio id, or null for the default. Null until the list
   * has loaded and still holds the stored choice, so a stale id is never sent.
   */
  readonly selectedId = computed(() => {
    const id = this.chosen();
    if (!id || this.stateSignal() !== 'ready') return null;
    return this.optionsSignal().some((p) => p.id === id) ? id : null;
  });
  /** The chosen portfolio (or the default one) when the list is known. */
  readonly current = computed<PortfolioRef | null>(() => {
    const list = this.optionsSignal();
    const id = this.selectedId();
    return (
      (id ? list.find((p) => p.id === id) : undefined) ??
      list.find((p) => p.is_default) ??
      (list.length === 1 ? list[0] : null)
    );
  });
  /** Real money: the shown portfolio trades live. Brass is for this only. */
  readonly live = computed(() => this.current()?.trading === 'live');
  /** Worth showing a picker: more than one portfolio to choose from. */
  readonly hasChoice = computed(() => this.optionsSignal().length > 1);
  /**
   * The user has a portfolio to show (UX-13). Money pages check this before
   * reading: false only once the list has loaded empty. A server without
   * the list route (`missing`) keeps the old behaviour and counts as true.
   */
  readonly hasBook = computed(() => {
    const state = this.stateSignal();
    return state !== 'ready' || this.optionsSignal().length > 0;
  });
  /** The list has loaded and is empty: send the trader to open a portfolio. */
  readonly noBook = computed(
    () => this.stateSignal() === 'ready' && this.optionsSignal().length === 0,
  );

  constructor() {
    inject(DestroyRef).onDestroy(() => this.clearRetry());
  }

  /**
   * Read the list once, and again after a failure. `force` starts a new
   * read even while one is running (after a change). Never throws.
   */
  load(force = false): Promise<void> {
    const state = this.stateSignal();
    if (!force && (state === 'ready' || state === 'missing')) return Promise.resolve();
    if (!force && this.loading) return this.loading;
    const run = this.fetch().finally(() => {
      if (this.loading === run) this.loading = null;
    });
    this.loading = run;
    return run;
  }

  select(id: string | null): void {
    const value = id || null;
    this.chosen.set(value);
    writeStored(this.userId(), value);
  }

  /** `{ portfolio_id }` for a read's query, or `{}` for the default portfolio. */
  query(): { portfolio_id?: string } {
    const id = this.selectedId();
    return id ? { portfolio_id: id } : {};
  }

  private async fetch(): Promise<void> {
    const generation = ++this.generation;
    this.clearRetry();
    // A re-read of a known list keeps showing it (no flash of the empty picker).
    if (this.stateSignal() !== 'ready') this.stateSignal.set('loading');
    try {
      const list = await this.api.list();
      if (generation !== this.generation) return;
      this.failures = 0;
      this.optionsSignal.set(list);
      this.stateSignal.set('ready');
      const id = this.chosen();
      if (id && !list.some((p) => p.id === id)) this.select(null);
    } catch (err) {
      if (generation !== this.generation) return;
      const missing = err instanceof ApiError && (err.status === 404 || err.status === 405);
      this.optionsSignal.set([]);
      this.stateSignal.set(missing ? 'missing' : 'failed');
      if (!missing) this.scheduleRetry();
    }
  }

  private scheduleRetry(): void {
    const wait = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** this.failures);
    this.failures += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.load();
    }, wait);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
}

/** The pick is kept per user, so a shared browser never mixes two traders' books. */
function storageKey(userId: string | null): string {
  return userId ? `${STORAGE_KEY}.${userId}` : STORAGE_KEY;
}

function readStored(userId: string | null): string | null {
  try {
    return localStorage.getItem(storageKey(userId));
  } catch {
    return null;
  }
}

function writeStored(userId: string | null, value: string | null): void {
  try {
    if (value) localStorage.setItem(storageKey(userId), value);
    else localStorage.removeItem(storageKey(userId));
  } catch {
    // Storage blocked: the choice lasts until reload.
  }
}
