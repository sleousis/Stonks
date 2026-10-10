import type { InsightsView, RiskPolicy } from '../../api/models';
import { formatNumber, formatPercent } from '../../core/format/format';

/** How close one measure is to its limit. */
export type LimitStatus = 'ok' | 'near' | 'over' | 'none';

export interface LimitRow {
  key: string;
  label: string;
  now: string;
  limit: string;
  /** now / limit, for the meter (null without a limit or a reading). */
  usage: number | null;
  status: LimitStatus;
  /** Glossary key for the help tip. */
  help?: string;
  /** The limit is your own, stricter than the system one. */
  yours?: boolean;
}

/** A reading this share of its limit or more counts as near. */
export const NEAR_SHARE = 0.8;

type Fmt = (n: number | null | undefined) => string;
const pct: Fmt = (n) => formatPercent(n, { digits: 1 });
const count: Fmt = (n) => formatNumber(n, { digits: 0 });

function status(now: number | null, limit: number | null): LimitStatus {
  if (limit == null || !(limit > 0)) return 'none';
  if (now == null) return 'ok';
  if (now > limit) return 'over';
  return now >= limit * NEAR_SHARE ? 'near' : 'ok';
}

function row(
  key: string,
  label: string,
  now: number | null,
  limit: number | null,
  fmt: Fmt,
  help?: string,
): LimitRow {
  const hasLimit = limit != null && limit > 0;
  return {
    key,
    label,
    now: fmt(now),
    limit: hasLimit ? fmt(limit) : 'No limit',
    usage: hasLimit && now != null ? Math.max(0, now / limit) : null,
    status: status(now, hasLimit ? limit : null),
    help,
  };
}

const CLASS_WORDS: Record<string, string> = {
  equity: 'equities',
  crypto: 'crypto',
  commodity: 'commodities',
  bond: 'bonds',
};

function classWord(cls: string): string {
  return CLASS_WORDS[cls] ?? cls;
}

function weightOf(slices: { key: string; weight: number | null }[], key: string): number | null {
  return slices.find((s) => s.key === key)?.weight ?? null;
}

function largest(slices: { key: string; weight: number | null }[]): number | null {
  const weights = slices.filter((s) => s.key !== 'cash' && s.weight != null).map((s) => s.weight!);
  return weights.length ? Math.max(...weights) : null;
}

/** Your own limits that are stricter than the system's, by policy key. */
function stricter(policy: RiskPolicy | null, system: RiskPolicy | null): Set<string> {
  const out = new Set<string>();
  if (!policy || !system) return out;
  for (const key of ['max_weight_per_ticker', 'max_open_positions'] as const) {
    const mine = policy[key];
    const base = system[key];
    if (mine != null && (base == null || mine < base)) out.add(key);
  }
  const cash = policy.cash_buffer_fraction ?? 0;
  if (cash > (system.cash_buffer_fraction ?? 0)) out.add('cash_buffer_fraction');
  return out;
}

/**
 * Each measure of the portfolio next to the limit it trades under, so a
 * trader sees how close it is before a rule clips an order or a halt trips.
 * `policy` is the limit that applies: your own limits where they are
 * stricter (`GET /api/risk/limits`, `effective`). Pass `system` too and the
 * rows where your own limit applies say so. Any input may be missing (still
 * loading or failed): its rows then show no reading.
 */
export function limitRows(
  insights: InsightsView | null,
  policy: RiskPolicy | null,
  system: RiskPolicy | null = null,
): LimitRow[] {
  const yours = stricter(policy, system);
  const rules = policy?.rules ?? {};
  const conc = insights?.risk.concentration ?? null;
  const alloc = insights?.allocation ?? null;
  const exposure = insights?.exposure ?? null;
  const stats = insights?.risk.holdings ?? insights?.risk.history ?? null;
  const drawdown = insights?.risk.history?.current_drawdown ?? null;
  // A cap of 100% of the book caps nothing.
  const cap = policy?.max_weight_per_ticker ?? null;
  const maxTicker = cap != null && cap < 1 ? cap : null;

  const topWeight = row('top_weight', 'Largest holding', conc?.top_weight ?? null, maxTicker, pct);
  topWeight.yours = yours.has('max_weight_per_ticker');
  const positions = row(
    'positions',
    'Open positions',
    conc ? conc.holdings : null,
    policy?.max_open_positions ?? null,
    count,
  );
  positions.yours = yours.has('max_open_positions');
  const rows: LimitRow[] = [
    topWeight,
    positions,
    row(
      'sector',
      'Largest sector',
      alloc ? largest(alloc.sector) : null,
      rules.sector_cap?.max_weight_per_sector ?? null,
      pct,
    ),
  ];
  const cashFloor = policy?.cash_buffer_fraction ?? 0;
  if (cashFloor > 0) {
    // A floor, not a cap: under it is the problem.
    const cash = alloc ? (weightOf(alloc.asset_class, 'cash') ?? 0) : null;
    const below = cash != null && cash < cashFloor;
    rows.push({
      key: 'cash',
      label: 'Cash kept',
      now: pct(cash),
      limit: `at least ${pct(cashFloor)}`,
      usage: cash == null ? null : below ? 1 : Math.min(1, cashFloor / Math.max(cash, 1e-9)),
      status: cash == null ? 'ok' : below ? 'over' : 'ok',
      yours: yours.has('cash_buffer_fraction'),
    });
  }
  for (const [cls, cap] of Object.entries(policy?.max_weight_per_asset_class ?? {})) {
    const now = alloc ? (weightOf(alloc.asset_class, cls) ?? 0) : null;
    rows.push(row(`class:${cls}`, `Weight in ${classWord(cls)}`, now, cap, pct));
  }
  rows.push(
    row(
      'gross',
      'Gross exposure',
      exposure?.gross ?? null,
      rules.gross_exposure?.max_gross ?? null,
      pct,
      'gross_exposure',
    ),
  );
  const net = row(
    'net',
    'Net exposure',
    exposure?.net ?? null,
    rules.net_exposure?.max_net ?? null,
    pct,
    'net_exposure',
  );
  const minNet = rules.net_exposure?.min_net ?? null;
  if (minNet != null) {
    net.limit = `${pct(minNet)} to ${net.status === 'none' ? 'any' : net.limit}`;
    if (exposure?.net != null && exposure.net < minNet) net.status = 'over';
    else if (net.status === 'none') net.status = 'ok';
  }
  rows.push(net);
  rows.push(
    row(
      'volatility',
      'Volatility, per year',
      stats?.volatility ?? null,
      rules.portfolio_vol?.vol_cap ?? null,
      pct,
      'volatility',
    ),
    row(
      'drawdown',
      'Drawdown from the peak',
      drawdown == null ? null : Math.abs(drawdown),
      rules.circuit_breaker?.max_drawdown_halt ?? null,
      pct,
      'drawdown',
    ),
  );
  return rows;
}

/** Plain words for a measure that has no limit, in the order they show. */
const NO_LIMIT_WORDS: Record<string, string> = {
  top_weight: 'the largest holding',
  positions: 'open positions',
  sector: 'sector',
  gross: 'exposure',
  net: 'exposure',
  volatility: 'volatility',
  drawdown: 'drawdown',
};

/** "No limit on sector, exposure, volatility or drawdown.", or '' when every row has one. */
export function noLimitText(rows: LimitRow[]): string {
  const words = [
    ...new Set(
      rows
        .filter((r) => r.status === 'none')
        .map((r) => NO_LIMIT_WORDS[r.key] ?? r.label.toLowerCase()),
    ),
  ];
  if (words.length === 0) return '';
  const list =
    words.length === 1 ? words[0] : `${words.slice(0, -1).join(', ')} or ${words.at(-1)}`;
  return `No limit on ${list}.`;
}

/** Words for a status, shown next to the meter (never colour alone). */
export const STATUS_TEXT: Record<LimitStatus, string> = {
  ok: 'Within limit',
  near: 'Near the limit',
  over: 'Over the limit',
  none: 'No limit set',
};
