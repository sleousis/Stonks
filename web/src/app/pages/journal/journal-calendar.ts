import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { JournalService } from '../../api/journal.service';
import { formatMoney, isoDay, toneClass } from '../../core/format/format';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatTile } from '../../shared/ui/stat-tile';
import { monthGrid, monthName, shiftMonth } from './journal-format';

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
    <section class="panel" aria-labelledby="cal-title">
      <div class="panel-head">
        <h2 id="cal-title">P&amp;L calendar</h2>
        <div class="month-nav">
          <button
            type="button"
            class="btn btn-ghost"
            (click)="month.set(prev())"
            [attr.aria-label]="'Show ' + label(prev())"
          >
            Previous
          </button>
          <span class="month-name" aria-live="polite">{{ label(month()) }}</span>
          <button
            type="button"
            class="btn btn-ghost"
            (click)="month.set(next())"
            [attr.aria-label]="'Show ' + label(next())"
          >
            Next
          </button>
        </div>
      </div>

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
          title="No closed trades yet"
          message="Each day fills in when a trade closes."
        />
      } @else {
        @let cal = calendar.value();
        <div class="tiles" aria-label="Calendar totals">
          <app-stat-tile
            label="Realised"
            [value]="money(cal.total)"
            [detail]="cal.trades + ' closed'"
            [help]="false"
            featured
          />
          <app-stat-tile label="This month" [value]="money(monthTotal())" [help]="false" />
          <app-stat-tile label="Best day" [value]="cal.best_day ?? 'n/a'" [help]="false" />
          <app-stat-tile label="Worst day" [value]="cal.worst_day ?? 'n/a'" [help]="false" />
        </div>
        @if (cal.unconverted > 0) {
          <p class="lead">
            {{ cal.unconverted }} trades are left out: no exchange rate from
            {{ cal.fx_missing.join(', ') }} to {{ cal.base_currency }}.
          </p>
        }
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
              <th scope="col">Week</th>
            </tr>
          </thead>
          <tbody>
            @for (w of weeks(); track $index) {
              <tr>
                @for (c of w.cells; track $index) {
                  <td [class.pad]="c.iso === null" [class]="tone(c.pnl)">
                    @if (c.iso) {
                      <span class="day">{{ c.day }}</span>
                      @if (c.pnl !== null) {
                        <span class="amt num">{{ money(c.pnl) }}</span>
                        <span class="visually-hidden">{{ c.trades }} trades</span>
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
          <h3 class="sub-title">By month</h3>
          <ul class="months">
            @for (m of cal.months; track m.key) {
              <li>
                <button type="button" class="btn btn-ghost" (click)="month.set(m.key)">
                  {{ label(m.key) }}
                </button>
                <span class="num" [class]="tone(m.pnl)">{{ money(m.pnl) }}</span>
                <span class="muted">{{ m.trades }} closed, {{ m.wins }} won</span>
              </li>
            }
          </ul>
        }
      }
    </section>
  `,
})
export class JournalCalendar {
  private readonly journal = inject(JournalService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly weekdays = WEEKDAYS;
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
