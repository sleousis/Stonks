import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

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
 * Live engine: is the intraday engine running, is its price stream
 * connected, how fast does it decide, and did the silent-engine alarm go
 * off. Below it, each of your portfolios' intraday P&L from the engine's
 * live marks (`GET /api/risk/intraday`), when those rows are kept.
 */
@Component({
  selector: 'app-live-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, StatusPill, EmptyState, ErrorState, LoadingState, UpdatedAgo],
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
