import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import type { EngineView } from '../../api/models';
import { StreamService } from '../../api/stream.service';
import { formatAgo, formatDateTime, formatNumber } from '../../core/format/format';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import {
  engineState,
  errorLines,
  latencySummary,
  latencyText,
  secondsText,
  streamWords,
} from './live-state';

/** The engine decides every minute, so read along more often than other pages. */
export const LIVE_REFRESH_MS = 15_000;

/**
 * Live engine: is the intraday engine running, is its price stream
 * connected, how fast does it decide, and did the silent-engine alarm go
 * off. Intraday P&L per portfolio shows here once live marks exist.
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

  protected readonly status = resource({ loader: () => this.api.status() });
  protected readonly auto = autoRefresh(() => [this.status], { everyMs: LIVE_REFRESH_MS });

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
  protected readonly count = (n: number) => formatNumber(n, { digits: 0 });

  protected when(at: string | null | undefined): string {
    return at ? `${formatAgo(at)}, ${formatDateTime(at)}` : 'Never';
  }

  protected refresh(): void {
    this.auto.refresh();
  }
}
