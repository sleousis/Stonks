import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { CalendarsService } from '../../api/calendars.service';
import type { EarningsWarning } from '../../api/models';
import { formatDate, formatDateTime } from '../../core/format/format';
import { timingLabel } from '../calendar/calendar-view';

/** A ticker worth asking about: `AAPL.US`, `BRK-B.US`, `BTC-USD.CC`. */
const TICKER = /^[A-Z0-9][A-Z0-9.-]{0,30}\.[A-Z]{1,6}$/;

/** "Earnings on 2026-10-29, after the close, before the next open (2026-10-30 13:30)." */
export function warningText(w: EarningsWarning): string {
  const when = timingLabel(w.before_after_market).toLowerCase();
  const timing = w.before_after_market ? `, ${when}` : '';
  return (
    `${w.ticker} reports earnings on ${formatDate(w.report_date)}${timing}, ` +
    `before the next open (${formatDateTime(w.next_open)}). ` +
    'An order placed now fills after the report, and the price can jump.'
  );
}

/**
 * The order ticket's warning line: the ticker reports earnings between now
 * and the next open, so an order placed now fills after the report. Shows
 * nothing when there is no report, and nothing when the check fails (the
 * call is silent, so a failure never blocks the ticket).
 */
@Component({
  selector: 'app-earnings-warning',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (warning(); as w) {
      <p class="warning" role="status">
        <span class="mark" aria-hidden="true">!</span>
        <span class="text">
          <strong>Earnings before the next open.</strong> {{ text(w) }}
          <a [routerLink]="['/calendar']" [queryParams]="{ ticker: w.ticker, date: w.report_date }"
            >See the calendar</a
          >
        </span>
      </p>
    }
  `,
  styles: `
    :host {
      display: block;
    }
    .warning {
      display: flex;
      gap: var(--space-2);
      align-items: flex-start;
      padding: var(--space-2) var(--space-3);
      border-left: 3px solid var(--color-warn);
      border-radius: var(--radius-sm);
      background: var(--color-warn-soft);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .mark {
      display: inline-grid;
      place-items: center;
      flex: none;
      width: 1.25rem;
      height: 1.25rem;
      border: 2px solid var(--color-warn);
      border-radius: 50%;
      color: var(--color-warn);
      font-weight: var(--weight-bold);
      line-height: 1;
    }
    .text a {
      display: inline-block;
      min-height: 24px;
    }
  `,
})
export class EarningsWarningLine {
  private readonly api = inject(CalendarsService);

  /** The ticket's ticker, as typed. */
  readonly ticker = input<string>('');

  private readonly symbol = computed(() => {
    const t = this.ticker().trim().toUpperCase();
    return TICKER.test(t) ? t : null;
  });

  protected readonly check = resource({
    params: () => this.symbol() ?? undefined,
    loader: ({ params }) => this.api.earningsWarnings([params]),
  });

  protected readonly warning = computed<EarningsWarning | null>(() => {
    const t = this.symbol();
    if (!t || !this.check.hasValue()) return null;
    return this.check.value().warnings.find((w) => w.ticker === t) ?? null;
  });

  protected readonly text = warningText;
}
