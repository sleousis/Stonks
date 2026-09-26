// The RuleSpec document behind a rule draft (src/stonks/strategies/rules/spec.py),
// typed for the builder, plus pure helpers the builder and tests share. The API
// is the validator of record; the local checks here only give instant feedback.

export type PriceField = 'open' | 'high' | 'low' | 'close' | 'volume';
export type ComparisonOp = '<' | '<=' | '>' | '>=' | 'crosses_above' | 'crosses_below';
export type GroupType = 'all' | 'any';

export interface IndicatorDef {
  id: string;
  kind: string;
  period?: number;
  source?: PriceField;
}

export type Operand = { type: 'indicator'; id: string } | { type: 'constant'; value: number };

export interface CompareNode {
  type: 'compare';
  left: Operand;
  op: ComparisonOp;
  right: Operand;
}

export interface GroupNode {
  type: GroupType;
  conditions: ConditionNode[];
}

export interface NotNode {
  type: 'not';
  condition: ConditionNode;
}

export type ConditionNode = CompareNode | GroupNode | NotNode;

export interface RuleSpecDoc {
  version: 1;
  name: string;
  description: string;
  interval: string;
  universe: { asset_classes: string[]; tickers?: string[] | null };
  indicators: IndicatorDef[];
  entry: ConditionNode;
  exit: ConditionNode | null;
  exit_when_entry_false: boolean;
  rank: { by: string; order: 'desc' | 'asc' };
  sizing: { max_positions: number; top_k?: number | null; allocation: number };
  risk: { stop_loss_pct: number | null; take_profit_pct: number | null };
  // Unknown keys typed in the JSON view survive so the API can reject them.
  [extra: string]: unknown;
}

export const COMPARISON_OPS: readonly { value: ComparisonOp; label: string }[] = [
  { value: '<', label: '<' },
  { value: '<=', label: '≤' },
  { value: '>', label: '>' },
  { value: '>=', label: '≥' },
  { value: 'crosses_above', label: 'crosses above' },
  { value: 'crosses_below', label: 'crosses below' },
];

export const INTERVALS = ['1m', '5m', '15m', '30m', '1h', '4h', '12h', '1d', '1w'] as const;
export const DEFAULT_ASSET_CLASSES = ['equity', 'crypto', 'commodity', 'bond'] as const;
export const PRICE_FIELDS: readonly PriceField[] = ['open', 'high', 'low', 'close', 'volume'];
export const ID_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,31}$/;

// ---- indicator catalog (from GET /api/studio/schema) ----------------------

export interface IndicatorKind {
  kind: string;
  label: string;
  description: string;
  period: { min: number; max: number } | null;
  sources: readonly PriceField[] | null;
}

const KIND_LABELS: Record<string, string> = {
  close: 'Close',
  volume: 'Volume',
  sma: 'Simple moving average',
  ema: 'Exponential moving average',
  rsi: 'RSI',
  roc: 'Rate of change',
  trailing_return: 'Trailing return',
  zscore: 'Z-score',
  atr: 'Average true range',
  donchian_high: 'Donchian high',
  donchian_low: 'Donchian low',
};

/** Used until the schema loads, or if it fails; mirrors spec.py. */
export const FALLBACK_KINDS: readonly IndicatorKind[] = [
  kind('close', "The bar's close.", null, null),
  kind('volume', "The bar's volume.", null, null),
  kind('sma', 'Simple moving average of source over period bars.', [1, 1000], PRICE_FIELDS),
  kind('ema', 'Exponential moving average (span period) of source.', [1, 1000], PRICE_FIELDS),
  kind('rsi', 'Wilder RSI of closes (0..100).', [2, 1000], null),
  kind('roc', 'Fractional change of closes over period bars (0.05 = +5%).', [1, 1000], null),
  kind(
    'trailing_return',
    'Fractional change of closes over period bars (0.05 = +5%).',
    [1, 1000],
    null,
  ),
  kind('zscore', 'Rolling z-score of source over period bars.', [2, 1000], PRICE_FIELDS),
  kind('atr', 'Average true range: mean of the true range over period bars.', [1, 1000], null),
  kind('donchian_high', 'Highest high of the period bars before the current one.', [1, 1000], null),
  kind('donchian_low', 'Lowest low of the period bars before the current one.', [1, 1000], null),
];

function kind(
  k: string,
  description: string,
  period: [number, number] | null,
  sources: readonly PriceField[] | null,
): IndicatorKind {
  return {
    kind: k,
    label: KIND_LABELS[k] ?? k,
    description,
    period: period ? { min: period[0], max: period[1] } : null,
    sources,
  };
}

type JsonObject = Record<string, unknown>;

function obj(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

/** Indicator kinds and their parameter bounds, read from the rule spec JSON Schema. */
export function indicatorKindsFromSchema(schema: unknown): IndicatorKind[] {
  const defs = obj(obj(schema)?.['$defs']);
  if (!defs) return [...FALLBACK_KINDS];
  const kinds: IndicatorKind[] = [];
  for (const [name, raw] of Object.entries(defs)) {
    const def = obj(raw);
    const props = obj(def?.['properties']);
    const kindProp = obj(props?.['kind']);
    if (!name.endsWith('Indicator') || !props || !kindProp) continue;
    const values =
      typeof kindProp['const'] === 'string'
        ? [kindProp['const']]
        : Array.isArray(kindProp['enum'])
          ? (kindProp['enum'] as string[])
          : [];
    const period = obj(props['period']);
    const source = obj(props['source']);
    const description = String(def?.['description'] ?? '').replace(/``/g, '');
    for (const k of values) {
      kinds.push({
        kind: k,
        label: KIND_LABELS[k] ?? k,
        description,
        period: period
          ? { min: Number(period['minimum'] ?? 1), max: Number(period['maximum'] ?? 1000) }
          : null,
        sources: Array.isArray(source?.['enum']) ? (source['enum'] as PriceField[]) : null,
      });
    }
  }
  if (!kinds.length) return [...FALLBACK_KINDS];
  const order = FALLBACK_KINDS.map((k) => k.kind);
  const rank = (k: string) => (order.includes(k) ? order.indexOf(k) : order.length);
  return kinds.sort((a, b) => rank(a.kind) - rank(b.kind));
}

/** Asset classes the universe filter accepts, from the schema. */
export function assetClassesFromSchema(schema: unknown): string[] {
  const universe = obj(obj(obj(schema)?.['$defs'])?.['UniverseFilter']);
  const items = obj(obj(obj(universe?.['properties'])?.['asset_classes'])?.['items']);
  const values = items?.['enum'];
  return Array.isArray(values) && values.length ? (values as string[]) : [...DEFAULT_ASSET_CLASSES];
}

// ---- documents ------------------------------------------------------------

export function indicatorOperand(id: string): Operand {
  return { type: 'indicator', id };
}

export function constantOperand(value: number): Operand {
  return { type: 'constant', value };
}

export function compare(left: Operand, op: ComparisonOp, right: Operand): CompareNode {
  return { type: 'compare', left, op, right };
}

/** A fresh spec: close above its 50-bar average, ranked by 20-bar momentum. */
export function blankSpec(name = ''): RuleSpecDoc {
  return {
    version: 1,
    name,
    description: '',
    interval: '1d',
    universe: { asset_classes: ['equity'] },
    indicators: [
      { id: 'close', kind: 'close' },
      { id: 'sma50', kind: 'sma', period: 50 },
      { id: 'roc20', kind: 'roc', period: 20 },
    ],
    entry: {
      type: 'all',
      conditions: [compare(indicatorOperand('close'), '>', indicatorOperand('sma50'))],
    },
    exit: compare(indicatorOperand('close'), '<', indicatorOperand('sma50')),
    exit_when_entry_false: false,
    rank: { by: 'roc20', order: 'desc' },
    sizing: { max_positions: 5, allocation: 1 },
    risk: { stop_loss_pct: null, take_profit_pct: null },
  };
}

/**
 * Fill in anything a stored or hand-edited spec leaves out so the builder
 * can render it. Values that are present are kept as they are (even when
 * invalid) so the API can point at them; unknown keys are kept too.
 */
export function toDoc(raw: unknown): RuleSpecDoc {
  const src = obj(raw) ?? {};
  const base = blankSpec();
  const universe = obj(src['universe']);
  const rank = obj(src['rank']);
  const sizing = obj(src['sizing']);
  const risk = obj(src['risk']);
  return {
    ...base,
    ...src,
    version: 1,
    name: typeof src['name'] === 'string' ? src['name'] : '',
    description: typeof src['description'] === 'string' ? src['description'] : '',
    interval: typeof src['interval'] === 'string' ? src['interval'] : '1d',
    universe: {
      ...universe,
      asset_classes: Array.isArray(universe?.['asset_classes'])
        ? (universe['asset_classes'] as string[])
        : ['equity'],
    },
    indicators: Array.isArray(src['indicators'])
      ? (src['indicators'] as IndicatorDef[])
      : base.indicators,
    entry: (obj(src['entry']) as ConditionNode | null) ?? base.entry,
    exit: 'exit' in src ? ((obj(src['exit']) as ConditionNode | null) ?? null) : null,
    exit_when_entry_false: src['exit_when_entry_false'] === true,
    rank: { order: 'desc', ...rank, by: typeof rank?.['by'] === 'string' ? rank['by'] : '' },
    sizing: { ...base.sizing, ...sizing } as RuleSpecDoc['sizing'],
    risk: { ...base.risk, ...risk } as RuleSpecDoc['risk'],
  };
}

/** A new indicator of `kindName` with an id not yet used in the spec. */
export function newIndicator(kindName: string, kinds: readonly IndicatorKind[], taken: string[]) {
  const k = kinds.find((x) => x.kind === kindName);
  const ind: IndicatorDef = { id: '', kind: kindName };
  if (k?.period) ind.period = Math.max(k.period.min, kindName === 'rsi' ? 14 : 20);
  ind.id = uniqueId(`${kindName}${ind.period ?? ''}`, taken);
  return ind;
}

export function uniqueId(base: string, taken: readonly string[]): string {
  const clean = base.replace(/[^A-Za-z0-9_]/g, '_').replace(/^[^A-Za-z_]/, '_$&') || 'ind';
  if (!taken.includes(clean)) return clean;
  for (let n = 2; ; n++) if (!taken.includes(`${clean}_${n}`)) return `${clean}_${n}`;
}

/** Switch an indicator's kind, keeping the period when the new kind has one. */
export function changeKind(
  ind: IndicatorDef,
  kindName: string,
  kinds: readonly IndicatorKind[],
): IndicatorDef {
  const k = kinds.find((x) => x.kind === kindName);
  const next: IndicatorDef = { id: ind.id, kind: kindName };
  if (k?.period) {
    const p = ind.period ?? 20;
    next.period = Math.min(Math.max(p, k.period.min), k.period.max);
  }
  if (k?.sources && ind.source) next.source = ind.source;
  return next;
}

/** Rename an indicator and every reference to it (conditions, ranking). */
export function renameIndicator(doc: RuleSpecDoc, index: number, newId: string): RuleSpecDoc {
  const oldId = doc.indicators[index]?.id;
  const indicators = doc.indicators.map((ind, i) => (i === index ? { ...ind, id: newId } : ind));
  const clash = doc.indicators.some((ind, i) => i !== index && ind.id === newId);
  if (oldId === undefined || clash || oldId === newId) return { ...doc, indicators };
  const swap = (node: ConditionNode | null) =>
    node
      ? mapOperands(node, (o) =>
          o.type === 'indicator' && o.id === oldId ? { ...o, id: newId } : o,
        )
      : null;
  return {
    ...doc,
    indicators,
    entry: swap(doc.entry) as ConditionNode,
    exit: swap(doc.exit),
    rank: doc.rank.by === oldId ? { ...doc.rank, by: newId } : doc.rank,
  };
}

function mapOperands(node: ConditionNode, fn: (o: Operand) => Operand): ConditionNode {
  switch (node.type) {
    case 'compare':
      return { ...node, left: fn(node.left), right: fn(node.right) };
    case 'not':
      return { ...node, condition: mapOperands(node.condition, fn) };
    case 'all':
    case 'any':
      return { ...node, conditions: node.conditions.map((c) => mapOperands(c, fn)) };
    default:
      return node;
  }
}

// ---- condition tree editing (immutable) ------------------------------------

/** A comparison between the first two indicators (or the first and zero). */
export function defaultComparison(ids: readonly string[]): CompareNode {
  const left = indicatorOperand(ids[0] ?? '');
  const right = ids[1] ? indicatorOperand(ids[1]) : constantOperand(0);
  return compare(left, '>', right);
}

export function addCondition(group: GroupNode, child: ConditionNode): GroupNode {
  return { ...group, conditions: [...group.conditions, child] };
}

export function replaceCondition(group: GroupNode, index: number, child: ConditionNode): GroupNode {
  return { ...group, conditions: group.conditions.map((c, i) => (i === index ? child : c)) };
}

export function removeCondition(group: GroupNode, index: number): GroupNode {
  return { ...group, conditions: group.conditions.filter((_, i) => i !== index) };
}

export function setGroupType(group: GroupNode, type: GroupType): GroupNode {
  return { ...group, type };
}

export function negate(node: ConditionNode): NotNode {
  return { type: 'not', condition: node };
}

/** Turn a single comparison into an "all of" group so more can be added. */
export function toGroup(node: ConditionNode, type: GroupType = 'all'): GroupNode {
  return { type, conditions: [node] };
}

// ---- validation issues ----------------------------------------------------

export interface SpecIssue {
  path: string;
  message: string;
}

export type IssueMap = Readonly<Record<string, readonly string[]>>;

export function toIssueMap(issues: readonly SpecIssue[]): IssueMap {
  const map: Record<string, string[]> = {};
  for (const { path, message } of issues) (map[path] ??= []).push(message);
  return map;
}

/** Merge issue lists, dropping a message already reported at the same path. */
export function mergeIssues(...lists: (readonly SpecIssue[])[]): SpecIssue[] {
  const seen = new Set<string>();
  const out: SpecIssue[] = [];
  for (const issue of lists.flat()) {
    const key = `${issue.path}\u0000${issue.message}`;
    if (!seen.has(key)) {
      seen.add(key);
      out.push(issue);
    }
  }
  return out;
}

/** Instant checks for the fields the builder edits (ids, periods, sizing). */
export function localIssues(doc: RuleSpecDoc, kinds: readonly IndicatorKind[]): SpecIssue[] {
  const issues: SpecIssue[] = [];
  const seen = new Set<string>();
  doc.indicators.forEach((ind, i) => {
    const at = `indicators[${i}]`;
    if (!ID_PATTERN.test(ind.id ?? '')) {
      issues.push({
        path: `${at}.id`,
        message: 'Use letters, digits and _ (up to 32), not starting with a digit',
      });
    } else if (seen.has(ind.id)) {
      issues.push({ path: `${at}.id`, message: `duplicate indicator id '${ind.id}'` });
    }
    seen.add(ind.id);
    const k = kinds.find((x) => x.kind === ind.kind);
    if (!k) {
      issues.push({ path: `${at}.kind`, message: `unknown indicator kind '${ind.kind}'` });
    } else if (k.period) {
      const p = ind.period;
      if (p === undefined || p === null || !Number.isInteger(p)) {
        issues.push({ path: `${at}.period`, message: 'Enter a whole number of bars' });
      } else if (p < k.period.min || p > k.period.max) {
        issues.push({
          path: `${at}.period`,
          message: `Between ${k.period.min} and ${k.period.max} bars`,
        });
      }
    }
  });
  if (!doc.universe.asset_classes.length) {
    issues.push({ path: 'universe.asset_classes', message: 'Pick at least one asset class' });
  }
  if (!seen.has(doc.rank.by)) {
    issues.push({ path: 'rank.by', message: `unknown indicator '${doc.rank.by}'` });
  }
  return issues;
}

/** Issues whose path is not rendered by any field fall back to the summary. */
export function describePath(path: string): string {
  if (!path) return 'Spec';
  return path
    .replace(/^indicators\[(\d+)\]/, (_, i) => `Indicator ${Number(i) + 1}`)
    .replace(/conditions\[(\d+)\]/g, (_, i) => `condition ${Number(i) + 1}`)
    .replace(/\./g, ' › ');
}
