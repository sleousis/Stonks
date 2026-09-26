// Canned Strategy Studio responses for tests.
import type { Draft } from '../app/api/models';

export function makeDraft(overrides: Partial<Draft> = {}): Draft {
  return {
    id: 'draft_abc123',
    name: 'RSI dip buyer',
    kind: 'rule',
    spec: structuredClone(RSI_TEMPLATE_SPEC),
    source_code: null,
    status: 'draft',
    registered_strategy_id: null,
    strategy_status: null,
    created_at: '2026-09-25T10:00:00Z',
    updated_at: '2026-09-25T10:00:00Z',
    ...overrides,
  };
}

/** The RSI mean-reversion template as GET /api/studio/templates returns it. */
export const RSI_TEMPLATE_SPEC = {
  version: 1,
  name: 'RSI mean reversion',
  interval: '1d',
  universe: { asset_classes: ['equity'] },
  indicators: [
    { id: 'rsi14', kind: 'rsi', period: 14 },
    { id: 'sma50', kind: 'sma', period: 50 },
    { id: 'close', kind: 'close' },
  ],
  entry: {
    type: 'all',
    conditions: [
      {
        type: 'compare',
        left: { type: 'indicator', id: 'rsi14' },
        op: '<',
        right: { type: 'constant', value: 30 },
      },
      {
        type: 'compare',
        left: { type: 'indicator', id: 'close' },
        op: '>',
        right: { type: 'indicator', id: 'sma50' },
      },
    ],
  },
  exit: {
    type: 'compare',
    left: { type: 'indicator', id: 'rsi14' },
    op: '>',
    right: { type: 'constant', value: 55 },
  },
  exit_when_entry_false: false,
  rank: { by: 'rsi14', order: 'asc' },
  sizing: { max_positions: 5, allocation: 1.0 },
  risk: { stop_loss_pct: 0.08, take_profit_pct: null },
};

/** Trimmed JSON Schema from GET /api/studio/schema. */
export const SCHEMA = {
  $defs: {
    SmaIndicator: {
      description: 'Simple moving average of ``source`` over ``period`` bars.',
      properties: {
        id: { type: 'string' },
        kind: { const: 'sma' },
        period: { minimum: 1, maximum: 1000, type: 'integer' },
        source: { enum: ['open', 'high', 'low', 'close', 'volume'], default: 'close' },
      },
    },
    RsiIndicator: {
      description: 'Wilder RSI of closes (0..100).',
      properties: {
        id: { type: 'string' },
        kind: { const: 'rsi' },
        period: { minimum: 2, maximum: 1000, type: 'integer' },
      },
    },
    RocIndicator: {
      description: 'Fractional change.',
      properties: {
        id: { type: 'string' },
        kind: { enum: ['roc', 'trailing_return'] },
        period: { minimum: 1, maximum: 1000, type: 'integer' },
      },
    },
    CloseIndicator: {
      description: "The bar's close.",
      properties: { id: { type: 'string' }, kind: { const: 'close' } },
    },
    UniverseFilter: {
      properties: { asset_classes: { items: { enum: ['equity', 'crypto'] } } },
    },
  },
};
