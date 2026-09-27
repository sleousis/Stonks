import type {
  OptionChainRow,
  OptionQuoteView,
  OptionsBacktestRequest,
  PayoffLegView,
  PayoffPointView,
} from '../../api/models';
import { formatDate, formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import type { TableColumn } from '../../shared/ui/data-table/data-table';
import { humanize } from '../../shared/ui/param-form/param-spec';

/** Which side of the chain the table shows. */
export type ChainSide = 'both' | 'calls' | 'puts';

type Field = 'bid' | 'ask' | 'iv' | 'delta' | 'gamma' | 'theta' | 'vega' | 'open_interest';

const FIELDS: readonly { key: Field; label: string; show: (v: number | null) => string }[] = [
  { key: 'bid', label: 'Bid', show: (v) => formatNumber(v, { digits: 2 }) },
  { key: 'ask', label: 'Ask', show: (v) => formatNumber(v, { digits: 2 }) },
  { key: 'iv', label: 'IV', show: (v) => formatPercent(v, { digits: 1 }) },
  { key: 'delta', label: 'Delta', show: (v) => formatNumber(v, { digits: 2 }) },
  { key: 'gamma', label: 'Gamma', show: (v) => formatNumber(v, { digits: 3 }) },
  { key: 'theta', label: 'Theta', show: (v) => formatNumber(v, { digits: 3 }) },
  { key: 'vega', label: 'Vega', show: (v) => formatNumber(v, { digits: 3 }) },
  { key: 'open_interest', label: 'Open interest', show: (v) => formatNumber(v, { digits: 0 }) },
];

function sideColumns(
  side: 'call' | 'put',
  prefix: string,
  hideOnPhone: readonly Field[],
): TableColumn<OptionChainRow>[] {
  return FIELDS.map((f) => {
    const pick = (row: OptionChainRow): number | null => {
      const q: OptionQuoteView | null | undefined = row[side];
      return q ? (q[f.key] ?? null) : null;
    };
    return {
      key: `${side}:${f.key}`,
      label: prefix ? `${prefix} ${f.label.toLowerCase()}` : f.label,
      format: 'number',
      help: false,
      value: pick,
      display: (row) => f.show(pick(row)),
      mobile: hideOnPhone.includes(f.key) ? 'hide' : 'show',
    } satisfies TableColumn<OptionChainRow>;
  });
}

/**
 * The chain table's columns: the strike (the card title on phones), then
 * bid, ask, IV and the Greeks for calls, puts or both. With both sides,
 * phones keep bid, ask and delta of each so a card stays short.
 */
export function chainColumns(side: ChainSide): TableColumn<OptionChainRow>[] {
  const strike: TableColumn<OptionChainRow> = {
    key: 'strike',
    label: 'Strike',
    format: 'number',
    mobile: 'title',
    help: false,
    display: (row) => formatNumber(row.strike, { digits: 2 }),
  };
  if (side === 'calls') return [strike, ...sideColumns('call', '', [])];
  if (side === 'puts') return [strike, ...sideColumns('put', '', [])];
  const trim: Field[] = ['iv', 'gamma', 'theta', 'vega', 'open_interest'];
  return [strike, ...sideColumns('call', 'Call', trim), ...sideColumns('put', 'Put', trim)];
}

/** "Bull call spread" from `bull_call_spread`. */
export function structureLabel(name: string): string {
  return humanize(name);
}

const PARAM_LABELS: Record<string, string> = {
  dte: 'Days to expiry',
  delta: 'Delta',
  long_delta: 'Long leg delta',
  short_delta: 'Short leg delta',
  wing_delta: 'Wing delta',
};

export function paramLabel(name: string): string {
  return PARAM_LABELS[name] ?? humanize(name);
}

/** "Sell 1 call 105, 2026-02-20" or "Hold 100 shares of AAPL.US". */
export function legText(leg: PayoffLegView): string {
  const qty = Math.abs(leg.quantity);
  if (leg.kind === 'shares') {
    return `${leg.quantity >= 0 ? 'Hold' : 'Short'} ${formatNumber(qty, { digits: 0 })} shares of ${leg.instrument}`;
  }
  const verb = leg.quantity >= 0 ? 'Buy' : 'Sell';
  const expiry = leg.expiry ? `, ${formatDate(leg.expiry)}` : '';
  return `${verb} ${formatNumber(qty, { digits: 0 })} ${leg.right ?? ''} ${formatNumber(leg.strike, { digits: 2 })}${expiry}`;
}

/** "Unlimited" for a missing bound, money otherwise. */
export function boundText(value: number | null | undefined): string {
  return value === null || value === undefined ? 'Unlimited' : formatMoney(value);
}

/** "Debit $250.00" or "Credit $200.00". */
export function costText(cost: number): string {
  return cost >= 0 ? `Debit ${formatMoney(cost)}` : `Credit ${formatMoney(-cost)}`;
}

// ---- the payoff diagram ------------------------------------------------------------

export interface PayoffGeometry {
  /** The profit line, as an SVG path. */
  line: string;
  /** Shaded areas between the line and zero, above and below it. */
  gain: string;
  loss: string;
  /** The y of zero profit, and the x of the spot today. */
  zeroY: number;
  spotX: number | null;
  xTicks: { x: number; label: string }[];
  yTicks: { y: number; label: string }[];
}

export interface Box {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export const PAYOFF_BOX: Box = {
  width: 640,
  height: 280,
  left: 64,
  right: 16,
  top: 12,
  bottom: 28,
};

function ticks(lo: number, hi: number, count: number): number[] {
  if (hi <= lo) return [lo];
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(8));
  return out;
}

function round(n: number): string {
  return n.toFixed(1);
}

/**
 * Scale payoff points into the box. The y range always includes zero, so
 * the gain and loss areas meet on the zero line.
 */
export function payoffGeometry(
  points: readonly PayoffPointView[],
  spot: number | null,
  box: Box = PAYOFF_BOX,
): PayoffGeometry | null {
  if (points.length < 2) return null;
  const xs = points.map((p) => p.spot);
  const ys = points.map((p) => p.profit);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  let y0 = Math.min(0, ...ys);
  let y1 = Math.max(0, ...ys);
  if (y1 - y0 < 1e-9) {
    y0 -= 1;
    y1 += 1;
  }
  const pad = (y1 - y0) * 0.06;
  y0 -= pad;
  y1 += pad;
  const w = box.width - box.left - box.right;
  const h = box.height - box.top - box.bottom;
  const xOf = (x: number) => box.left + ((x - x0) / (x1 - x0 || 1)) * w;
  const yOf = (y: number) => box.top + (1 - (y - y0) / (y1 - y0)) * h;
  const zeroY = yOf(0);
  const line = points
    .map((p, i) => `${i ? 'L' : 'M'}${round(xOf(p.spot))},${round(yOf(p.profit))}`)
    .join(' ');
  const area = (clip: (v: number) => number) =>
    `M${round(xOf(x0))},${round(zeroY)} ` +
    points.map((p) => `L${round(xOf(p.spot))},${round(yOf(clip(p.profit)))}`).join(' ') +
    ` L${round(xOf(x1))},${round(zeroY)} Z`;
  return {
    line,
    gain: area((v) => Math.max(v, 0)),
    loss: area((v) => Math.min(v, 0)),
    zeroY,
    spotX: spot !== null && spot >= x0 && spot <= x1 ? xOf(spot) : null,
    xTicks: ticks(x0, x1, 6).map((v) => ({ x: xOf(v), label: formatNumber(v, { digits: 0 }) })),
    yTicks: ticks(y0, y1, 4).map((v) => ({
      y: yOf(v),
      label: formatMoney(v, { compact: true }),
    })),
  };
}

// ---- the backtest form ---------------------------------------------------------------

export interface BacktestForm {
  strategy: string;
  underlyings: string;
  start: string;
  end: string;
  cash: number | null;
  validation: boolean;
}

export function defaultBacktestForm(): BacktestForm {
  return { strategy: '', underlyings: '', start: '', end: '', cash: 100_000, validation: true };
}

export function splitTickers(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[\s,]+/)) {
    const t = raw.trim().toUpperCase();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

export const MAX_UNDERLYINGS = 10;

/** Field → what is wrong, in words, before anything is sent. */
export function backtestErrors(f: BacktestForm): Record<string, string> {
  const out: Record<string, string> = {};
  if (!f.strategy) out['strategy'] = 'Pick a strategy.';
  const tickers = splitTickers(f.underlyings);
  if (!tickers.length) out['underlyings'] = 'Name at least one underlying, such as AAPL.US.';
  else if (tickers.length > MAX_UNDERLYINGS)
    out['underlyings'] = `At most ${MAX_UNDERLYINGS} underlyings.`;
  if (!f.start) out['start'] = 'Pick the first day.';
  if (!f.end) out['end'] = 'Pick the last day.';
  else if (f.start && f.start > f.end) out['end'] = 'The last day comes after the first.';
  if (f.cash === null || !(f.cash > 0)) out['cash'] = 'Starting cash must be above zero.';
  return out;
}

export function buildBacktestRequest(
  f: BacktestForm,
  params: Record<string, unknown>,
): OptionsBacktestRequest {
  return {
    strategy: f.strategy,
    underlyings: splitTickers(f.underlyings),
    start: f.start,
    end: f.end,
    cash: f.cash ?? 100_000,
    params,
    validation: f.validation,
  };
}

const CHECK_LABELS: Record<string, string> = {
  oos: 'Out of sample',
  deflated_sharpe: 'Deflated Sharpe',
  fill_stress: 'Wider fills',
  missing_quotes: 'Missing quote days',
  cost_stress: 'Doubled fees',
};

/** A validation check's display name. */
export function checkLabel(id: string): string {
  return CHECK_LABELS[id] ?? humanize(id);
}
