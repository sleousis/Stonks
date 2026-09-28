import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';

import { LiveService } from '../../api/live.service';
import type { MarginView } from '../../api/models';
import { formatDateTime, formatMoney, formatPercent } from '../../core/format/format';
import { marginLevelWords } from '../../shared/live-rules';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

/** How full the margin meter is: maintenance margin over equity, 0 to 1. */
export function marginMeter(v: Pick<MarginView, 'account'>): number {
  const use = v.account?.margin_use ?? 0;
  return Math.min(Math.max(use, 0), 1);
}

/** The pattern day trader state in one line, or null when it does not bind. */
export function pdtText(v: Pick<MarginView, 'pdt'>): string | null {
  const p = v.pdt;
  if (!p.applies) return null;
  const left =
    p.day_trades_remaining == null
      ? 'The broker sent no count.'
      : `${p.day_trades_remaining} left now.`;
  return (
    `Under ${formatMoney(p.equity_threshold, { currency: 'USD' })}: at most ` +
    `${p.max_day_trades} day trades in ${p.window_days} trading days. ${left}`
  );
}

/**
 * Buying power and margin use of a live portfolio (roadmap 19.13), read
 * from the broker each time the page opens. A cash account shows its
 * buying power only. A margin account also shows how much of its equity
 * the maintenance margin uses, its cushion before a margin call, and how
 * much new margin the safety buffer still allows.
 */
@Component({
  selector: 'app-live-margin-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill, ErrorState, LoadingState],
  templateUrl: './live-margin-panel.html',
  styleUrl: './live-margin-panel.scss',
})
export class LiveMarginPanel {
  private readonly live = inject(LiveService);

  readonly portfolioId = input.required<string>();

  protected readonly margin = resource({
    params: () => ({ id: this.portfolioId() }),
    loader: ({ params }) => this.live.margin(params.id),
  });

  protected readonly view = computed(() => (this.margin.hasValue() ? this.margin.value() : null));
  protected readonly isMargin = computed(() => this.view()?.account?.account_type === 'margin');
  protected readonly level = computed(() => marginLevelWords(this.view()?.account?.level));
  protected readonly meter = computed(() => {
    const v = this.view();
    return v ? marginMeter(v) : 0;
  });
  protected readonly pdt = computed(() => {
    const v = this.view();
    return v ? pdtText(v) : null;
  });

  protected readonly money = formatMoney;
  protected readonly pct = formatPercent;
  protected readonly dateTime = formatDateTime;
}
