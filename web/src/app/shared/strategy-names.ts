/**
 * Names for strategies that a trader can read (UX-27). Registered ids look
 * like `stocks_on_the_move_3fa9c21b` (a slug of the kind or draft name plus
 * eight hex characters). Pages show the display name and keep the id under
 * "Technical details".
 */

/** A slug and the registry's eight-character hex suffix. */
const GENERATED_ID = /^(.+?)_([0-9a-f]{8})$/;

export interface DisplayNameHints {
  /** The Studio draft's own name, when the strategy came from a draft. */
  draftName?: string | null;
  /** A starter strategy's plain title and summary (`starter` on the API's views). */
  starter?: { title: string; summary?: string } | null;
  /** The plain title the API sends as `strategy_name` next to a strategy id. */
  name?: string | null;
}

/**
 * A snake_case id such as `etf_trend` or `starter_buy_and_hold`. A word may
 * hold a hyphen (a ticker such as `btc-usd`) once the id has an underscore,
 * so `momentum-v3` stays as it is.
 */
const SNAKE_ID = /^(?:[a-z0-9]+(?:_[a-z0-9]+)*|[a-z0-9-]+(?:_[a-z0-9-]+)+)$/;

/** Words that read as capitals in a name. */
const ACRONYMS: Readonly<Record<string, string>> = {
  etf: 'ETF',
  etfs: 'ETFs',
  rsi: 'RSI',
  sma: 'SMA',
  ema: 'EMA',
  macd: 'MACD',
  atr: 'ATR',
  ath: 'ATH',
  vwap: 'VWAP',
  orb: 'ORB',
  ewmac: 'EWMAC',
  tsmom: 'TSMOM',
  kama: 'KAMA',
  ic: 'IC',
  ml: 'ML',
  us: 'US',
  eu: 'EU',
  uk: 'UK',
  vix: 'VIX',
  hrp: 'HRP',
};

/** `etf_trend` -> `ETF trend`, `bah_btc-usd` -> `Bah BTC-USD`. */
function humanise(snake: string): string {
  const words = snake
    .split(/_+/)
    .filter(Boolean)
    .map((w) => (w.includes('-') ? w.toUpperCase() : (ACRONYMS[w] ?? w)));
  const text = words.join(' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * `stocks_on_the_move_3fa9c21b` -> `Stocks on the move 3fa9`, `etf_trend`
 * -> `ETF trend`. A starter's title (or the API's `strategy_name`), then a
 * draft name, wins when known, with no hex suffix. Any other snake_case id
 * reads as words. Anything else is shown as it is.
 */
export function strategyDisplayName(id: string, hints: DisplayNameHints = {}): string {
  const starter = hints.starter?.title.trim() || hints.name?.trim();
  if (starter) return starter;
  const draft = hints.draftName?.trim();
  if (draft) return draft;
  const match = GENERATED_ID.exec(id);
  if (match && SNAKE_ID.test(match[1])) return `${humanise(match[1])} ${match[2].slice(0, 4)}`;
  if (match) return `${match[1]} ${match[2].slice(0, 4)}`;
  return SNAKE_ID.test(id) ? humanise(id) : id;
}

/** A row's strategy name from the API's `strategy_id` and `strategy_name`; null without a strategy. */
export function rowStrategyName(row: {
  strategy_id?: string | null;
  strategy_name?: string | null;
}): string | null {
  return row.strategy_id ? strategyDisplayName(row.strategy_id, { name: row.strategy_name }) : null;
}

/** `stonks.strategies.momentum.MomentumStrategy` or `pkg.mod:Class` -> the class name. */
export function shortClassName(classPath: string): string {
  return classPath.split(/[.:]/).at(-1) || classPath;
}

/**
 * A readable name for the strategy's kind, from its class:
 * `stonks.strategies.momentum.MomentumStrategy` -> `Momentum strategy`,
 * `RSIMeanReversion` -> `RSI mean reversion`.
 */
export function strategyKindName(classPath: string): string {
  const words = shortClassName(classPath)
    .replace(/_/g, ' ')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
    .split(/\s+/);
  return words
    .map((w, i) => {
      if (w.length > 1 && w === w.toUpperCase()) return w;
      return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w.toLowerCase();
    })
    .join(' ');
}
