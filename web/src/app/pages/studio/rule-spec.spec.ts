import {
  type GroupNode,
  addCondition,
  compare,
  constantOperand,
  indicatorKindsFromSchema,
  indicatorOperand,
  localIssues,
  mergeIssues,
  negate,
  removeCondition,
  renameIndicator,
  replaceCondition,
  setGroupType,
  toDoc,
  toGroup,
  toIssueMap,
  FALLBACK_KINDS,
  blankSpec,
  describePath,
} from './rule-spec';
import { RSI_TEMPLATE_SPEC, SCHEMA } from '../../../testing/studio-fixtures';

describe('rule spec helpers', () => {
  it('round-trips a template spec unchanged', () => {
    expect(toDoc(RSI_TEMPLATE_SPEC)).toEqual({ ...RSI_TEMPLATE_SPEC, description: '' });
  });

  it('fills a partial spec with defaults but keeps unknown keys for the API', () => {
    const doc = toDoc({ indicators: [{ id: 'x', kind: 'close' }], bogus: 1 });
    expect(doc.version).toBe(1);
    expect(doc.interval).toBe('1d');
    expect(doc.indicators).toEqual([{ id: 'x', kind: 'close' }]);
    expect(doc.exit).toBeNull();
    expect(doc['bogus']).toBe(1);
  });

  it('reads indicator kinds and parameter bounds from the JSON schema', () => {
    const kinds = indicatorKindsFromSchema(SCHEMA);
    expect(kinds.map((k) => k.kind)).toEqual(['close', 'sma', 'rsi', 'roc', 'trailing_return']);
    const rsi = kinds.find((k) => k.kind === 'rsi');
    expect(rsi?.period).toEqual({ min: 2, max: 1000 });
    expect(rsi?.sources).toBeNull();
    expect(kinds.find((k) => k.kind === 'sma')?.sources).toContain('high');
    expect(kinds.find((k) => k.kind === 'sma')?.description).not.toContain('``');
    expect(indicatorKindsFromSchema(null)).toEqual([...FALLBACK_KINDS]);
  });

  describe('condition groups', () => {
    const a = compare(indicatorOperand('rsi14'), '<', constantOperand(30));
    const b = compare(indicatorOperand('close'), '>', indicatorOperand('sma50'));
    const group: GroupNode = { type: 'all', conditions: [a] };

    it('adds, replaces and removes children without mutating the original', () => {
      const added = addCondition(group, b);
      expect(added.conditions).toEqual([a, b]);
      expect(group.conditions).toEqual([a]);

      const swapped = replaceCondition(added, 1, negate(b));
      expect(swapped.conditions[1]).toEqual({ type: 'not', condition: b });

      expect(removeCondition(swapped, 0).conditions).toEqual([{ type: 'not', condition: b }]);
    });

    it('switches between all and any, and wraps a comparison in a group', () => {
      expect(setGroupType(group, 'any')).toEqual({ type: 'any', conditions: [a] });
      expect(toGroup(b)).toEqual({ type: 'all', conditions: [b] });
    });
  });

  it('renames an indicator everywhere it is referenced', () => {
    const doc = toDoc(RSI_TEMPLATE_SPEC);
    const next = renameIndicator(doc, 0, 'rsi_fast');
    expect(next.indicators[0].id).toBe('rsi_fast');
    expect(next.rank.by).toBe('rsi_fast');
    expect(JSON.stringify(next.entry)).toContain('"rsi_fast"');
    expect(JSON.stringify(next.exit)).toContain('"rsi_fast"');
    expect(JSON.stringify(next)).not.toContain('"rsi14"');
  });

  it('does not merge references into an id another indicator already uses', () => {
    const doc = toDoc(RSI_TEMPLATE_SPEC);
    const next = renameIndicator(doc, 0, 'sma50');
    expect(next.rank.by).toBe('rsi14');
  });

  it('flags ids, periods and ranking locally before the API answers', () => {
    const doc = blankSpec();
    doc.indicators = [
      { id: '9bad', kind: 'close' },
      { id: 'rsi', kind: 'rsi', period: 1 },
      { id: 'rsi', kind: 'sma', period: 20 },
    ];
    doc.rank = { by: 'missing', order: 'desc' };
    const map = toIssueMap(localIssues(doc, indicatorKindsFromSchema(SCHEMA)));
    expect(map['indicators[0].id']).toBeDefined();
    expect(map['indicators[1].period']).toEqual(['Between 2 and 1000 bars']);
    expect(map['indicators[2].id']?.[0]).toContain('duplicate');
    expect(map['rank.by']?.[0]).toContain('missing');
  });

  it('merges API and local issues without duplicates, keyed by path', () => {
    const merged = mergeIssues(
      [{ path: 'rank.by', message: 'x' }],
      [
        { path: 'rank.by', message: 'x' },
        { path: 'entry', message: 'y' },
      ],
    );
    expect(merged).toHaveLength(2);
    expect(toIssueMap(merged)).toEqual({ 'rank.by': ['x'], entry: ['y'] });
  });

  it('describes issue paths in words', () => {
    expect(describePath('indicators[1].period')).toBe('Indicator 2 › period');
    expect(describePath('entry.conditions[0].left.id')).toBe('entry › condition 1 › left › id');
  });
});
