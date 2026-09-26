import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { SystemService } from '../../api/system.service';
import { PageHeader } from '../../shared/ui/page-header';
import { type CoveragePick, CoveragePanel } from './coverage-panel';
import { IngestPanel } from './ingest-panel';
import { IngestRunsPanel } from './ingest-runs-panel';
import { InstrumentSearch } from './instrument-search';
import { PricePanel } from './price-panel';

const FALLBACK_INTERVALS = [{ code: '1d', is_intraday: false, seconds: 86_400 }];

/**
 * What is in the lake and how fresh it is, plus ingest. The page owns the
 * selected ticker and interval; each panel owns its own resource so one
 * failing route never blanks the rest.
 */
@Component({
  selector: 'app-data-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, InstrumentSearch, CoveragePanel, PricePanel, IngestPanel, IngestRunsPanel],
  templateUrl: './data.page.html',
  styleUrl: './data.page.scss',
})
export class DataPage {
  private readonly system = inject(SystemService);

  /** Query param `?instrument=` (e.g. from the command palette) preselects a ticker. */
  readonly instrument = input<string | undefined>();
  protected readonly ticker = linkedSignal<string | null>(() => this.instrument() ?? null);
  protected readonly interval = signal('1d');
  /** Bumped after an ingest or on Refresh; panels refetch when it changes. */
  protected readonly refreshKey = signal(0);

  private readonly search = viewChild(InstrumentSearch);

  protected readonly intervalInfo = resource({ loader: () => this.system.intervals() });
  protected readonly intervals = computed(() =>
    this.intervalInfo.hasValue() && this.intervalInfo.value().length
      ? this.intervalInfo.value()
      : FALLBACK_INTERVALS,
  );
  protected readonly intervalCodes = computed(() => this.intervals().map((i) => i.code));

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
