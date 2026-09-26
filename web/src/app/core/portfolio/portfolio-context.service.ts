import { Injectable, computed, inject, signal } from '@angular/core';

import { type PortfolioRef, PortfoliosService } from '../../api/portfolios.service';
import { ApiError } from '../http/api-error';

const STORAGE_KEY = 'stonks.portfolio';

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
 * - The choice is remembered per browser, and dropped if the portfolio is
 *   no longer in the list.
 */
@Injectable({ providedIn: 'root' })
export class PortfolioContextService {
  private readonly api = inject(PortfoliosService);

  private readonly optionsSignal = signal<PortfolioRef[]>([]);
  private readonly stateSignal = signal<'idle' | 'loading' | 'ready' | 'missing' | 'failed'>(
    'idle',
  );
  /** The stored choice; only used once the list confirms it still exists. */
  private readonly chosen = signal<string | null>(readStored());
  private loading: Promise<void> | null = null;

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
  /** Worth showing a picker: more than one portfolio to choose from. */
  readonly hasChoice = computed(() => this.optionsSignal().length > 1);

  /** Read the list once (`force` reads again). Never throws. */
  load(force = false): Promise<void> {
    if (!force && this.stateSignal() !== 'idle') return this.loading ?? Promise.resolve();
    this.loading ??= this.fetch().finally(() => (this.loading = null));
    return this.loading;
  }

  select(id: string | null): void {
    const value = id || null;
    this.chosen.set(value);
    writeStored(value);
  }

  /** `{ portfolio_id }` for a read's query, or `{}` for the default portfolio. */
  query(): { portfolio_id?: string } {
    const id = this.selectedId();
    return id ? { portfolio_id: id } : {};
  }

  private async fetch(): Promise<void> {
    this.stateSignal.set('loading');
    try {
      const list = await this.api.list();
      this.optionsSignal.set(list);
      this.stateSignal.set('ready');
      const id = this.chosen();
      if (id && !list.some((p) => p.id === id)) this.select(null);
    } catch (err) {
      const missing = err instanceof ApiError && (err.status === 404 || err.status === 405);
      this.optionsSignal.set([]);
      this.stateSignal.set(missing ? 'missing' : 'failed');
    }
  }
}

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStored(value: string | null): void {
  try {
    if (value) localStorage.setItem(STORAGE_KEY, value);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage blocked: the choice lasts until reload.
  }
}
