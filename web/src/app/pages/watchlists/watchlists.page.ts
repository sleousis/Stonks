import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { WatchlistView } from '../../api/models';
import { MarketService } from '../../api/market.service';
import { WatchlistsService } from '../../api/watchlists.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { NumPipe, PctPipe } from '../../shared/format.pipes';
import { PageHeader } from '../../shared/ui/page-header';
import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { parseTickerText } from '../welcome/welcome-steps';
import { Sparkline } from './sparkline';

/** A ticker's last close, its day change and the last 60 closes. */
export interface TickerQuote {
  ticker: string;
  last: number | null;
  change: number | null;
  closes: number[];
}

/** The quote of one ticker from its recent daily bars (oldest first). */
export function quoteFrom(
  ticker: string,
  bars: readonly { close: number | null; adj_close?: number | null }[],
): TickerQuote {
  const closes = bars
    .map((b) => b.adj_close ?? b.close)
    .filter((c): c is number => c != null && Number.isFinite(c));
  const last = closes.at(-1) ?? null;
  const prev = closes.length > 1 ? closes[closes.length - 2] : null;
  const change = last != null && prev ? last / prev - 1 : null;
  return { ticker, last, change, closes };
}

/** `/lab?tickers=A,B`: the lab forms start from these tickers. */
export function labLink(w: Pick<WatchlistView, 'tickers'>): { tickers: string } {
  return { tickers: w.tickers.join(',') };
}

/**
 * Your watchlists: named ticker lists. Each opens in the lab as a quick
 * universe, each ticker opens its chart, and Today and the charts can show
 * only one list.
 */
@Component({
  selector: 'app-watchlists-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PageTabs,
    PermissionNote,
    LoadingState,
    EmptyState,
    ErrorState,
    Sparkline,
    NumPipe,
    PctPipe,
  ],
  templateUrl: './watchlists.page.html',
  styleUrl: './watchlists.page.scss',
})
export class WatchlistsPage {
  private readonly api = inject(WatchlistsService);
  private readonly market = inject(MarketService);
  private readonly ctx = inject(WatchlistContextService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  protected readonly session = inject(SessionService);

  protected readonly lists = resource({ loader: () => this.api.list() });
  protected readonly canEdit = computed(() => this.session.can('portfolio.manage'));

  /** The list shown: the one picked, else the first. */
  protected readonly selectedId = linkedSignal<WatchlistView[] | undefined, string | null>({
    source: () => (this.lists.hasValue() ? this.lists.value() : undefined),
    computation: (lists, previous) => {
      if (!lists?.length) return null;
      const kept = previous?.value && lists.some((l) => l.id === previous.value);
      return kept ? previous!.value : lists[0].id;
    },
  });
  protected readonly selected = computed(() => {
    const id = this.selectedId();
    return this.lists.hasValue() ? (this.lists.value().find((l) => l.id === id) ?? null) : null;
  });
  protected readonly tabs = computed<PageTab[]>(() =>
    this.lists.hasValue()
      ? this.lists.value().map((l) => ({ id: l.id, label: l.name, badge: l.tickers.length }))
      : [],
  );

  /** The picked list's tickers with their last close, day change and 60 closes. */
  protected readonly quotes = resource({
    params: () => this.selected()?.tickers,
    loader: async ({ params: tickers }) => {
      const rows = await Promise.all(
        tickers.map(async (t) => {
          try {
            const series = await this.market.bars({ ticker: t, interval: '1d', limit: 60 });
            return quoteFrom(t, series.bars);
          } catch {
            return quoteFrom(t, []);
          }
        }),
      );
      return new Map(rows.map((r) => [r.ticker, r]));
    },
  });

  protected quote(ticker: string): TickerQuote | null {
    return this.quotes.hasValue() ? (this.quotes.value().get(ticker) ?? null) : null;
  }

  /** "3 lists, 24 tickers. Tech rose most today at +1.20%." */
  protected readonly standfirst = computed(() => {
    if (!this.lists.hasValue()) return 'Your own ticker lists, to chart, test and filter Today.';
    const lists = this.lists.value();
    if (!lists.length) return 'Save the tickers you follow, then chart them or test them in the lab.';
    const tickers = new Set(lists.flatMap((l) => l.tickers)).size;
    const head = `${lists.length} ${lists.length === 1 ? 'list' : 'lists'}, ${tickers} ${tickers === 1 ? 'ticker' : 'tickers'}.`;
    const list = this.selected();
    if (!list || !this.quotes.hasValue()) return `${head} Pick a list to see its day.`;
    const moves = [...this.quotes.value().values()].filter((q) => q.change != null);
    if (!moves.length) return `${head} No prices yet for ${list.name}.`;
    const up = moves.filter((q) => (q.change ?? 0) > 0).length;
    return `${head} In ${list.name}, ${up} of ${moves.length} rose in the last session.`;
  });

  protected readonly newName = signal('');
  protected readonly newTickers = signal('');
  protected readonly newError = signal<string | null>(null);
  protected readonly creating = signal(false);

  /** The list being edited, with its draft name and tickers. */
  protected readonly editing = signal<{ id: string; name: string; tickers: string } | null>(null);
  protected readonly editError = signal<string | null>(null);
  protected readonly saving = signal(false);

  protected readonly labLink = labLink;

  protected async create(event: Event): Promise<void> {
    event.preventDefault();
    const name = this.newName().trim();
    const tickers = parseTickerText(this.newTickers());
    if (!name) return this.newError.set('Give the watchlist a name.');
    this.creating.set(true);
    this.newError.set(null);
    try {
      const made = await this.api.create({ name, tickers });
      this.newName.set('');
      this.newTickers.set('');
      this.toasts.success(`Saved ${made.name}.`);
      this.refresh();
    } catch (err) {
      if (err instanceof ApiError) this.newError.set(err.message);
    } finally {
      this.creating.set(false);
    }
  }

  protected edit(w: WatchlistView): void {
    this.editError.set(null);
    this.editing.set({ id: w.id, name: w.name, tickers: w.tickers.join(', ') });
  }

  protected patchEdit(patch: Partial<{ name: string; tickers: string }>): void {
    this.editing.update((e) => (e ? { ...e, ...patch } : e));
  }

  protected async save(event: Event): Promise<void> {
    event.preventDefault();
    const e = this.editing();
    if (!e) return;
    const name = e.name.trim();
    if (!name) return this.editError.set('Give the watchlist a name.');
    this.saving.set(true);
    this.editError.set(null);
    try {
      const saved = await this.api.update(e.id, { name, tickers: parseTickerText(e.tickers) });
      this.editing.set(null);
      this.toasts.success(`Saved ${saved.name}.`);
      this.refresh();
    } catch (err) {
      if (err instanceof ApiError) this.editError.set(err.message);
    } finally {
      this.saving.set(false);
    }
  }

  protected async remove(w: WatchlistView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Delete ${w.name}?`,
      message: 'The list goes for good. Your trades and strategies do not change.',
      confirmLabel: 'Delete watchlist',
      tone: 'danger',
    });
    if (!ok) return;
    try {
      await this.api.delete(w.id);
      if (this.ctx.selectedId() === w.id) this.ctx.select(null);
      this.toasts.success(`Deleted ${w.name}.`);
      this.refresh();
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  private refresh(): void {
    this.lists.reload();
    void this.ctx.load(true);
  }
}
