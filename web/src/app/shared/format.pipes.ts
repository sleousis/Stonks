import { Pipe, type PipeTransform } from '@angular/core';

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
@Pipe({ name: 'money' })
export class MoneyPipe implements PipeTransform {
  transform(value: number | null | undefined, opts?: NumberOptions): string {
    return formatMoney(value, opts);
  }
}

/** Fractions to percent: {{ 0.012 | pct }} → 1.20% */
@Pipe({ name: 'pct' })
export class PctPipe implements PipeTransform {
  transform(value: number | null | undefined, opts?: NumberOptions): string {
    return formatPercent(value, opts);
  }
}

@Pipe({ name: 'num' })
export class NumPipe implements PipeTransform {
  transform(value: number | null | undefined, opts?: NumberOptions): string {
    return formatNumber(value, opts);
  }
}

@Pipe({ name: 'day' })
export class DayPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatDate(value);
  }
}

@Pipe({ name: 'dateTime' })
export class DateTimePipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatDateTime(value);
  }
}

/** Not pure-reactive to the clock; fine for data refreshed on load. */
@Pipe({ name: 'ago' })
export class AgoPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatAgo(value);
  }
}

export const FORMAT_PIPES = [MoneyPipe, PctPipe, NumPipe, DayPipe, DateTimePipe, AgoPipe] as const;
