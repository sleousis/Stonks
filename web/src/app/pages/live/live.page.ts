import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { EngineView } from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { StreamService } from '../../api/stream.service';
import {
  formatAgo,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  toneClass,
} from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import {
  type PnlLine,
  engineState,
  errorLines,
  latencySummary,
  latencyText,
  secondsText,
  staleMarksText,
  streamWords,
} from './live-state';

/** The engine decides every minute, so read along more often than other pages. */
export const LIVE_REFRESH_MS = 15_000;

/**
 * Intraday engine: is the intraday engine running, is its price stream
 * connected, how fast does it decide, and did the silent-engine alarm go
 * off. Below it, each of your portfolios' intraday P&L from the engine's
 * live marks (`GET /api/risk/intraday`), when those rows are kept.
 */
@Component({
  selector: 'app-live-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, StatusPill, EmptyState, ErrorState, LoadingState, UpdatedAgo],
  templateUrl: './live.page.html',
  styleUrl: './live.page.scss',
})
export class LivePage {
  private readonly api = inject(StreamService);
  private readonly portfolios = inject(PortfolioContextService);

  protected readonly status = resource({ loader: () => this.api.status() });

  /** Your portfolios' latest intraday rows, read only while they are kept. */
  protected readonly pnl = resource({
    params: () =>
      this.status.hasValue() && this.status.value().intraday_pnl.available
        ? { list: this.portfolios.options() }
        : undefined,
    loader: ({ params }) => this.loadPnl(params.list),
  });

  protected readonly auto = autoRefresh(() => [this.status, this.pnl], {
    everyMs: LIVE_REFRESH_MS,
  });

  protected readonly engines = computed<EngineView[]>(() =>
    this.status.hasValue() ? this.status.value().engines : [],
  );
  protected readonly troubled = computed(
    () => this.engines().filter((e) => engineState(e).tone === 'negative').length,
  );

  protected readonly state = engineState;
  protected readonly streamWords = streamWords;
  protected readonly seconds = secondsText;
  protected readonly latency = latencyText;
  protected readonly latencySummary = latencySummary;
  protected readonly errors = errorLines;
  protected readonly stale = staleMarksText;
  protected readonly tone = toneClass;
  protected readonly count = (n: number) => formatNumber(n, { digits: 0 });
  protected readonly money = (n: number) => formatMoney(n, { signed: true });
  protected readonly plainMoney = (n: number) => formatMoney(n);
  protected readonly percent = (n: number | null | undefined) =>
    formatPercent(n ?? null, { signed: true });

  /** One sentence from the data under the title. */
  protected readonly standfirst = computed(() => {
    if (!this.status.hasValue()) return 'Its price stream and how fast it decides.';
    const list = this.engines();
    if (!list.length) {
      return this.status.value().streaming_enabled
        ? 'Minute prices are on. No engine has reported yet.'
        : 'No engine runs. Intraday trading is off.';
    }
    const trouble = this.troubled();
    const lead =
      list.length === 1
        ? '1 engine reports.'
        : `${formatNumber(list.length, { digits: 0 })} engines report.`;
    if (trouble) return `${lead} ${trouble === 1 ? 'One needs' : `${trouble} need`} a look.`;
    const last = list
      .map((e) => e.last_dispatch_at)
      .filter((v): v is string => !!v)
      .sort()
      .at(-1);
    return last
      ? `${lead} The last bar closed ${formatAgo(last)}.`
      : `${lead} No bar has closed yet.`;
  });

  /** P&L today per portfolio as bars either side of zero, scaled to the largest. */
  protected readonly chart = computed(() => {
    if (!this.pnl.hasValue()) return [];
    const rows = this.pnl
      .value()
      .filter((l) => l.row)
      .map((l) => ({ id: l.id, name: l.name, value: l.row!.pnl }));
    const top = Math.max(0, ...rows.map((r) => Math.abs(r.value)));
    return rows.map((r) => ({ ...r, width: top > 0 ? (Math.abs(r.value) / top) * 100 : 0 }));
  });
  protected readonly chartSummary = computed(
    () =>
      'P&L today by portfolio: ' +
      this.chart()
        .map((b) => `${b.name} ${this.money(b.value)}`)
        .join(', '),
  );

  protected ago(at: string | null | undefined): string {
    return at ? formatAgo(at) : 'Never';
  }

  protected at(at: string | null | undefined): string {
    return at ? formatDateTime(at) : '';
  }

  protected when(at: string | null | undefined): string {
    return at ? `${formatAgo(at)}, ${formatDateTime(at)}` : 'Never';
  }

  protected refresh(): void {
    this.auto.refresh();
  }

  /** One line per portfolio. With no list yet, the default portfolio. */
  private async loadPnl(list: readonly PortfolioRef[]): Promise<PnlLine[]> {
    const targets = list.length
      ? list.map((p) => ({ id: p.id, name: p.name, query: p.id }))
      : [{ id: 'default', name: 'Your portfolio', query: undefined }];
    return Promise.all(
      targets.map(async (t) => {
        try {
          return { id: t.id, name: t.name, row: await this.api.latestPnl(t.query), failed: false };
        } catch {
          return { id: t.id, name: t.name, row: null, failed: true };
        }
      }),
    );
  }
}
