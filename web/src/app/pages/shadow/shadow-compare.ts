import type { PnlRowView } from '../../api/models';
import type { ChartPoint } from '../../shared/chart/chart-engine';

/**
 * Rebases a value series to 100 on `fromDay` (or the first day on or after
 * it), so series that started with different capital share one axis.
 * Rows before the base day are dropped; rows without value are skipped.
 */
export function normalizeTo100(rows: readonly PnlRowView[], fromDay?: string | null): ChartPoint[] {
  const sorted = [...rows]
    .filter((r) => Number.isFinite(r.total_value))
    .sort((a, b) => a.day.localeCompare(b.day));
  const start = sorted.findIndex((r) => (!fromDay || r.day >= fromDay) && r.total_value > 0);
  if (start < 0) return [];
  const base = sorted[start].total_value;
  // Rounded so float noise (110.00000000000001) never reaches legends or tests.
  return sorted
    .slice(start)
    .map((r) => ({ time: r.day, value: Math.round((r.total_value / base) * 1e8) / 1e6 }));
}

/** Return of a normalized series up to and including `toDay` (last point when omitted). */
export function returnOf(points: readonly ChartPoint[], toDay?: string | null): number | null {
  const upTo = toDay ? points.filter((p) => p.time <= toDay) : points;
  const last = upTo.at(-1);
  return last && points.length ? last.value / 100 - 1 : null;
}

export interface ShadowComparison {
  strategyId: string;
  firstDay: string | null;
  lastDay: string | null;
  daysElapsed: number;
  /** Shadow return since its first day. */
  shadowReturn: number | null;
  /** Real portfolio return over the same days. */
  realReturn: number | null;
  /** Shadow minus real over the same window. */
  excess: number | null;
  drawdown: number | null;
  maxDrawdown: number | null;
}

/**
 * A shadow strategy against the real portfolio over the shadow's own window:
 * both rebased to 100 on the shadow's first day, compared at its last day.
 */
export function compareToReal(
  strategyId: string,
  shadowRows: readonly PnlRowView[],
  realRows: readonly PnlRowView[],
): ShadowComparison {
  const shadow = normalizeTo100(shadowRows);
  const firstDay = shadow[0]?.time ?? null;
  const lastDay = shadow.at(-1)?.time ?? null;
  const real = firstDay ? normalizeTo100(realRows, firstDay) : [];
  const shadowReturn = returnOf(shadow);
  // The same days only: your portfolio must have started by the strategy's
  // first day. One that started later covers fewer days, so no comparison.
  const startedBy = !!firstDay && realRows.some((r) => r.day <= firstDay);
  const realReturn =
    startedBy && real.length && lastDay && real[0].time <= lastDay ? returnOf(real, lastDay) : null;
  const sortedRows = [...shadowRows].sort((a, b) => a.day.localeCompare(b.day));
  const last = sortedRows.at(-1);
  return {
    strategyId,
    firstDay,
    lastDay,
    daysElapsed: last?.days_elapsed ?? sortedRows.length,
    shadowReturn,
    realReturn,
    excess: shadowReturn !== null && realReturn !== null ? shadowReturn - realReturn : null,
    drawdown: last?.drawdown ?? null,
    maxDrawdown: sortedRows.length ? Math.min(...sortedRows.map((r) => r.drawdown)) : null,
  };
}

export interface ComparisonSeries {
  id: string;
  points: ChartPoint[];
}

function firstDayOf(rows: readonly PnlRowView[]): string {
  return rows.reduce((min, r) => (r.day < min ? r.day : min), rows[0].day);
}

/** The value of a series on `day`, or on the last day before it; null before it starts. */
function valueOn(points: readonly ChartPoint[], day: string): number | null {
  let value: number | null = null;
  for (const p of points) {
    if (p.time > day) break;
    value = p.value;
  }
  return value;
}

/**
 * Series for one chart (UX-25). Your portfolio starts at 100 on the first
 * paper day of any strategy. Each strategy starts on its own first day, at
 * your portfolio's value that day, so a strategy that started yesterday
 * never cuts the others' history short, and the gap between a strategy and
 * your portfolio is how far ahead or behind it is since it started.
 */
export function comparisonSeries(
  real: readonly PnlRowView[],
  shadows: readonly { id: string; rows: readonly PnlRowView[] }[],
): { baseDay: string | null; real: ChartPoint[]; shadows: ComparisonSeries[] } {
  const withData = shadows.filter((s) => s.rows.length > 0);
  if (withData.length === 0) return { baseDay: null, real: [], shadows: [] };
  const earliest = withData.map((s) => firstDayOf(s.rows)).reduce((a, b) => (b < a ? b : a));
  const realFirst = real.length ? firstDayOf(real) : null;
  const baseDay = realFirst && realFirst > earliest ? realFirst : earliest;
  const realPoints = normalizeTo100(real, baseDay);
  return {
    baseDay,
    real: realPoints,
    shadows: withData.map((s) => {
      const own = firstDayOf(s.rows);
      const start = own > baseDay ? own : baseDay;
      const anchor = valueOn(realPoints, start) ?? 100;
      return {
        id: s.id,
        points: normalizeTo100(s.rows, start).map((p) => ({
          time: p.time,
          value: Math.round(p.value * anchor * 1e4) / 1e6,
        })),
      };
    }),
  };
}
