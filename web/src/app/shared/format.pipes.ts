import { Pipe, type PipeTransform } from '@angular/core';

// The pipes are impure so a change of locale or time zone in Settings
// re-renders figures at once; the formatters cache their Intl objects, so a
// re-run costs a map lookup.

import {
  type NumberOptions,
  formatAgo,
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
} from '../core/format/format';

/** {{ value | money }} · {{ change | money: { signed: true } }} */
@Pipe({ name: 'money', pure: false })
export class MoneyPipe implements PipeTransform {
  transform(value: number | null | undefined, opts?: NumberOptions): string {
    return formatMoney(value, opts);
  }
}

/** Fractions to percent: {{ 0.012 | pct }} → 1.20% */
@Pipe({ name: 'pct', pure: false })
export class PctPipe implements PipeTransform {
  transform(value: number | null | undefined, opts?: NumberOptions): string {
    return formatPercent(value, opts);
  }
}

@Pipe({ name: 'num', pure: false })
export class NumPipe implements PipeTransform {
  transform(value: number | null | undefined, opts?: NumberOptions): string {
    return formatNumber(value, opts);
  }
}

@Pipe({ name: 'day', pure: false })
export class DayPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatDate(value);
  }
}

@Pipe({ name: 'dateTime', pure: false })
export class DateTimePipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatDateTime(value);
  }
}

/** Not reactive to the clock; fine for data refreshed on load. */
@Pipe({ name: 'ago' })
export class AgoPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatAgo(value);
  }
}

export const FORMAT_PIPES = [MoneyPipe, PctPipe, NumPipe, DayPipe, DateTimePipe, AgoPipe] as const;
