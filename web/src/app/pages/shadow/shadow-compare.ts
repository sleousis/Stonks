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
  // Real rows on or before the shadow's last day; none means no overlap.
  const realReturn =
    real.length && lastDay && real[0].time <= lastDay ? returnOf(real, lastDay) : null;
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

/**
 * Series for one chart: the real portfolio and each shadow strategy, all
 * rebased to 100 on the same day (the latest first day among them), so every
 * line starts together and gaps between them are real out- or
 * under-performance.
 */
export function comparisonSeries(
  real: readonly PnlRowView[],
  shadows: readonly { id: string; rows: readonly PnlRowView[] }[],
): { baseDay: string | null; real: ChartPoint[]; shadows: ComparisonSeries[] } {
  const withData = shadows.filter((s) => s.rows.length > 0);
  const firstDays = [real, ...withData.map((s) => s.rows)]
    .filter((rows) => rows.length > 0)
    .map((rows) => rows.reduce((min, r) => (r.day < min ? r.day : min), rows[0].day));
  if (withData.length === 0 || firstDays.length === 0) {
    return { baseDay: null, real: [], shadows: [] };
  }
  const baseDay = firstDays.reduce((max, d) => (d > max ? d : max));
  return {
    baseDay,
    real: normalizeTo100(real, baseDay),
    shadows: withData.map((s) => ({ id: s.id, points: normalizeTo100(s.rows, baseDay) })),
  };
}
