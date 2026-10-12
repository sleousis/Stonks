import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { JournalService } from '../../api/journal.service';
import { formatDate, formatMoney, isoDay, toneClass } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatTile } from '../../shared/ui/stat-tile';
import { monthGrid, monthName, plural, shiftMonth } from './journal-format';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/**
 * Realised P&L on the day each trade closed, one month at a time, with the
 * week's total at the end of each row and the months listed below. Money is
 * in the portfolio's base currency.
 */
@Component({
  selector: 'app-journal-calendar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatTile, EmptyState, ErrorState, LoadingState],
  styleUrl: './journal.scss',
  template: `
    <section class="page-section" aria-labelledby="cal-title">
      <header class="section-head">
        <h2 id="cal-title">P&amp;L calendar</h2>
        <div class="month-nav">
          <button
            type="button"
            class="btn btn-ghost btn-icon"
            (click)="month.set(prev())"
            [attr.aria-label]="'Show ' + label(prev())"
          >
            <svg
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              aria-hidden="true"
            >
              <path d="M10 3.5 5.5 8l4.5 4.5" />
            </svg>
          </button>
          <span class="month-name" aria-live="polite">{{ label(month()) }}</span>
          <button
            type="button"
            class="btn btn-ghost btn-icon"
            (click)="month.set(next())"
            [attr.aria-label]="'Show ' + label(next())"
          >
            <svg
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              aria-hidden="true"
            >
              <path d="M6 3.5 10.5 8 6 12.5" />
            </svg>
          </button>
        </div>
      </header>

      <div class="section-body">
        @if (calendar.error(); as err) {
          <app-error-state
            title="Could not load the calendar"
            [error]="err"
            (retry)="calendar.reload()"
          />
        } @else if (!calendar.hasValue()) {
          <app-loading-state label="Loading the calendar" [rows]="5" />
        } @else if (calendar.value().trades === 0 && calendar.value().unconverted === 0) {
          <app-empty-state
            icon="orders"
            title="No closed trades yet"
            message="Each day fills in when a trade closes."
          />
        } @else {
          @let cal = calendar.value();
          <div class="tiles summary-strip" aria-label="Calendar totals">
            <app-stat-tile
              label="Realised"
              [value]="money(cal.total)"
              [detail]="closedText(cal.trades)"
              [help]="false"
            />
            <app-stat-tile label="This month" [value]="money(monthTotal())" [help]="false" />
            <app-stat-tile
              label="Best day this month"
              [value]="best() ? money(best()!.pnl) : 'n/a'"
              [detail]="best() ? dayText(best()!.key) : null"
              [help]="false"
            />
            <app-stat-tile
              label="Worst day this month"
              [value]="worst() ? money(worst()!.pnl) : 'n/a'"
              [detail]="worst() ? dayText(worst()!.key) : null"
              [help]="false"
            />
          </div>
          @if (cal.unconverted > 0) {
            <p class="lead band-warn">
              {{ cal.unconverted }} trades are left out: no exchange rate from
              {{ cal.fx_missing.join(', ') }} to {{ cal.base_currency }}.
            </p>
          }
          <div class="cal-layout">
            <table class="cal">
              <caption class="visually-hidden">
                Realised P&amp;L by day in
                {{
                  label(month())
                }}, with the total of each week
              </caption>
              <thead>
                <tr>
                  @for (d of weekdays; track d) {
                    <th scope="col">{{ d }}</th>
                  }
                  <th scope="col" class="week-head">Week</th>
                </tr>
              </thead>
              <tbody>
                @for (w of weeks(); track $index) {
                  <tr>
                    @for (c of w.cells; track $index) {
                      <td
                        [class.pad]="c.iso === null"
                        [class.traded]="c.pnl !== null"
                        [class.today]="c.iso === today"
                        [class]="tone(c.pnl)"
                      >
                        @if (c.iso) {
                          <span class="day num">{{ c.day }}</span>
                          @if (c.pnl !== null) {
                            <span class="amt num">{{ money(c.pnl) }}</span>
                            <span class="visually-hidden">{{ count(c.trades, 'trade') }}</span>
                          }
                        }
                      </td>
                    }
                    <td class="week num" [class]="tone(w.total)">
                      {{ w.total === null ? '' : money(w.total) }}
                    </td>
                  </tr>
                }
              </tbody>
            </table>

            @if (cal.months.length > 0) {
              <div class="by-month">
                <h3 class="sub-title">By month</h3>
                <ul class="row-list months">
                  @for (m of cal.months; track m.key) {
                    <li [class.current]="m.key === month()">
                      <button type="button" class="month-pick" (click)="month.set(m.key)">
                        <span class="row-main">{{ label(m.key) }}</span>
                        <span class="row-meta">{{ m.trades }} closed, {{ m.wins }} won</span>
                      </button>
                      <span class="row-figure" [class]="tone(m.pnl)">{{ money(m.pnl) }}</span>
                    </li>
                  }
                </ul>
              </div>
            }
          </div>
        }
      </div>
    </section>
  `,
})
export class JournalCalendar {
  private readonly journal = inject(JournalService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly weekdays = WEEKDAYS;
  protected readonly today = isoDay();
  readonly month = signal(isoDay().slice(0, 7));

  protected readonly calendar = resource({
    params: () => ({ portfolio: this.ctx.selectedId() }),
    loader: () => this.journal.calendar(),
  });

  protected readonly prev = computed(() => shiftMonth(this.month(), -1));
  protected readonly next = computed(() => shiftMonth(this.month(), 1));

  protected readonly weeks = computed(() =>
    monthGrid(this.month(), this.calendar.hasValue() ? this.calendar.value().days : []),
  );

  protected readonly monthTotal = computed(() => {
    if (!this.calendar.hasValue()) return 0;
    return this.calendar.value().months.find((m) => m.key === this.month())?.pnl ?? 0;
  });

  /** The days of the month shown, so best and worst never come from another month. */
  private readonly monthDays = computed(() =>
    this.calendar.hasValue()
      ? this.calendar.value().days.filter((d) => d.key.startsWith(this.month()))
      : [],
  );
  protected readonly best = computed(() => {
    const days = this.monthDays().filter((d) => d.pnl > 0);
    return days.length ? days.reduce((a, b) => (b.pnl > a.pnl ? b : a)) : null;
  });
  protected readonly worst = computed(() => {
    const days = this.monthDays().filter((d) => d.pnl < 0);
    return days.length ? days.reduce((a, b) => (b.pnl < a.pnl ? b : a)) : null;
  });

  protected readonly count = plural;

  protected closedText(n: number): string {
    return `${plural(n, 'trade')} closed`;
  }

  protected dayText(iso: string): string {
    return formatDate(iso);
  }

  protected label(month: string): string {
    return monthName(month);
  }

  protected money(value: number | null | undefined): string {
    const base = this.calendar.hasValue() ? this.calendar.value().base_currency : 'USD';
    return formatMoney(value, { currency: base, signed: true });
  }

  protected tone(value: number | null): string {
    return value === null ? '' : toneClass(value);
  }
}
