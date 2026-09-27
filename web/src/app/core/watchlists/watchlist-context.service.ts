import { Injectable, computed, inject, signal } from '@angular/core';

import type { WatchlistView } from '../../api/models';
import { WatchlistsService } from '../../api/watchlists.service';

const STORAGE_KEY = 'stonks.watchlist';

/**
 * Your watchlists and the one picked as a filter (Today, the charts). The
 * pick is remembered per browser and dropped when the list is gone. Null
 * means "every ticker". Loading never throws: without the route, or signed
 * out, the list is simply empty.
 */
@Injectable({ providedIn: 'root' })
export class WatchlistContextService {
  private readonly api = inject(WatchlistsService);
  private readonly listSignal = signal<WatchlistView[]>([]);
  private readonly stateSignal = signal<'idle' | 'loading' | 'ready' | 'failed'>('idle');
  private readonly chosen = signal<string | null>(readStored());
  private loading: Promise<void> | null = null;

  readonly lists = this.listSignal.asReadonly();
  readonly state = this.stateSignal.asReadonly();
  /** The picked list, once loaded and still there. */
  readonly selected = computed<WatchlistView | null>(() => {
    const id = this.chosen();
    return (id && this.listSignal().find((w) => w.id === id)) || null;
  });
  readonly selectedId = computed(() => this.selected()?.id ?? null);
  /** Tickers of the picked list, or null for no filter. */
  readonly tickers = computed<ReadonlySet<string> | null>(() => {
    const w = this.selected();
    return w ? new Set(w.tickers) : null;
  });

  /** Read the lists once (`force` reads again). */
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

  /** Keep a ticker when no list is picked, or when the list holds it. */
  keeps(ticker: string): boolean {
    const set = this.tickers();
    return !set || set.has(ticker.toUpperCase());
  }

  private async fetch(): Promise<void> {
    this.stateSignal.set('loading');
    try {
      this.listSignal.set(await this.api.list(true));
      this.stateSignal.set('ready');
    } catch {
      this.listSignal.set([]);
      this.stateSignal.set('failed');
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
    // Private mode: the pick lasts for this tab only.
  }
}

/** The ticker a signal notification is about: its title reads "AAPL.US: entry signal". */
export function signalTicker(title: string): string | null {
  const i = title.indexOf(':');
  return i > 0 ? title.slice(0, i).trim().toUpperCase() : null;
}
