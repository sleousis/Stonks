import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { IngestRunView } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { SessionService } from '../../core/auth/session.service';
import { formatAgo } from '../../core/format/format';
import { PageHeader } from '../../shared/ui/page-header';
import { StatTile } from '../../shared/ui/stat-tile';
import { commonIntervals, kindLabel, runStatusLabel, updateOutcome } from './data-labels';
import { type CoveragePick, CoveragePanel } from './coverage-panel';
import { IngestPanel } from './ingest-panel';
import { IngestRunsPanel } from './ingest-runs-panel';
import { InstrumentSearch } from './instrument-search';
import { PricePanel } from './price-panel';

const FALLBACK_INTERVALS = [{ code: '1d', is_intraday: false, seconds: 86_400 }];

/**
 * The price data we hold and how fresh it is, plus data updates. The page owns the
 * selected ticker and interval; each panel owns its own resource so one
 * failing route never blanks the rest.
 */
@Component({
  selector: 'app-data-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    RouterLink,
    StatTile,
    InstrumentSearch,
    CoveragePanel,
    PricePanel,
    IngestPanel,
    IngestRunsPanel,
  ],
  templateUrl: './data.page.html',
  styleUrl: './data.page.scss',
})
export class DataPage {
  private readonly system = inject(SystemService);
  protected readonly session = inject(SessionService);
  private readonly host = inject(ElementRef<HTMLElement>);

  /** Query param `?instrument=` (e.g. from the command palette) preselects a ticker. */
  readonly instrument = input<string | undefined>();
  protected readonly ticker = linkedSignal<string | null>(() => this.instrument() ?? null);
  protected readonly interval = signal('1d');
  /** Bumped after a data update or on Refresh; panels refetch when it changes. */
  protected readonly refreshKey = signal(0);

  private readonly search = viewChild(InstrumentSearch);

  protected readonly intervalInfo = resource({ loader: () => this.system.intervals() });
  protected readonly intervals = computed(() =>
    this.intervalInfo.hasValue() && this.intervalInfo.value().length
      ? this.intervalInfo.value()
      : FALLBACK_INTERVALS,
  );
  protected readonly intervalCodes = computed(() => this.intervals().map((i) => i.code));
  /** The price chart offers the common intervals only, plus the one picked from coverage. */
  protected readonly priceIntervals = computed(() =>
    commonIntervals(this.intervalCodes(), this.interval()),
  );

  /** The newest data updates, as the updates table last read them unfiltered. */
  protected readonly recent = signal<IngestRunView[] | null>(null);
  protected readonly last = computed(() => this.recent()?.[0] ?? null);
  /** Recent updates where some tickers failed: their bad bars are set aside. */
  protected readonly troubled = computed(
    () =>
      (this.recent() ?? []).filter((r) => (r.tickers_failed ?? 0) > 0 || r.status === 'error')
        .length,
  );

  /** "The last update ran 2h ago. Updated 12 tickers." */
  protected readonly standfirst = computed(() => {
    const last = this.last();
    if (!last) return 'The price data we hold, how fresh it is, and the updates that fill it.';
    const what = kindLabel(last.kind).toLowerCase();
    if (last.status === 'running') return `An update of ${what} is running now.`;
    return `The last update of ${what} ran ${formatAgo(last.started_at)}. ${updateOutcome(last)}`;
  });

  protected readonly lastStatus = computed(() => runStatusLabel(this.last()?.status));
  protected readonly lastAgo = computed(() => formatAgo(this.last()?.started_at));

  /** Move to the update form and put the cursor in it. */
  protected goToUpdate(): void {
    const form = this.host.nativeElement.querySelector('#ingest-title') as HTMLElement | null;
    form?.scrollIntoView({ block: 'start' });
    const first = this.host.nativeElement.querySelector(
      'app-ingest-panel select, app-ingest-panel input, app-ingest-panel textarea',
    ) as HTMLElement | null;
    first?.focus({ preventScroll: true });
  }

  /** Move to the updates table. */
  protected goToRuns(): void {
    const runs = this.host.nativeElement.querySelector('#runs-title') as HTMLElement | null;
    runs?.scrollIntoView({ block: 'start' });
  }

  protected pickTicker(ticker: string): void {
    this.ticker.set(ticker);
  }

  protected pickSeries(pick: CoveragePick): void {
    this.ticker.set(pick.ticker);
    this.interval.set(pick.interval);
  }

  protected refresh(): void {
    this.refreshKey.update((n) => n + 1);
    this.search()?.reload();
    this.intervalInfo.reload();
  }
}
