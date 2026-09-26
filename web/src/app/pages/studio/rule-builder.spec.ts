import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { RSI_TEMPLATE_SPEC, SCHEMA } from '../../../testing/studio-fixtures';
import { RuleBuilder } from './rule-builder';
import {
  type GroupNode,
  type IssueMap,
  type RuleSpecDoc,
  indicatorKindsFromSchema,
  toDoc,
} from './rule-spec';

describe('RuleBuilder', () => {
  let fixture: ComponentFixture<RuleBuilder>;
  let el: HTMLElement;

  function setup(spec: RuleSpecDoc, issues: IssueMap = {}): void {
    TestBed.configureTestingModule({ imports: [RuleBuilder] });
    fixture = TestBed.createComponent(RuleBuilder);
    fixture.componentRef.setInput('spec', spec);
    fixture.componentRef.setInput('kinds', indicatorKindsFromSchema(SCHEMA));
    fixture.componentRef.setInput('assetClasses', ['equity', 'crypto']);
    fixture.componentRef.setInput('issues', issues);
    fixture.detectChanges();
    el = fixture.nativeElement;
  }

  const spec = () => fixture.componentInstance.spec();

  function change(target: Element | null, value: string, event = 'change'): void {
    const input = target as HTMLInputElement | HTMLSelectElement;
    input.value = value;
    input.dispatchEvent(new Event(event));
    fixture.detectChanges();
  }

  function button(label: string, root: ParentNode = el): HTMLButtonElement {
    const found = [...root.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === label || b.getAttribute('aria-label') === label,
    );
    if (!found) throw new Error(`no button "${label}"`);
    return found;
  }

  it('renders a template and emits the same spec JSON back after a no-op edit', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC));
    expect((el.querySelector('#rb-name') as HTMLInputElement).value).toBe('RSI mean reversion');
    expect(el.querySelectorAll('.indicator')).toHaveLength(3);
    expect((el.querySelector('#rb-ind-period-0') as HTMLInputElement).value).toBe('14');

    // Selects whose options come from @for still show the spec's values.
    const selected = (sel: string) => (el.querySelector(sel) as HTMLSelectElement).value;
    expect(selected('#rb-interval')).toBe('1d');
    expect(selected('#rb-ind-kind-0')).toBe('rsi');
    expect(selected('#rb-ind-source-1')).toBe('close');
    expect(selected('#rb-rank-by')).toBe('rsi14');
    const second = el.querySelector('[data-node="entry.conditions[1]"]') as HTMLElement;
    expect(selected('[data-node="entry.conditions[1]"] select[aria-label="Comparison"]')).toBe('>');
    expect(
      (second.querySelector('select[aria-label="Right side"]') as HTMLSelectElement).value,
    ).toBe('i:sma50');

    change(el.querySelector('#rb-ind-period-0'), '14');
    expect(JSON.parse(JSON.stringify(spec()))).toEqual({ ...RSI_TEMPLATE_SPEC, description: '' });
  });

  it('builds the expected spec JSON from builder edits', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC));

    change(el.querySelector('#rb-ind-period-0'), '7');
    change(el.querySelector('#rb-ind-id-0'), 'rsi7', 'input');
    change(el.querySelector('#rb-rank-order'), 'desc');
    change(el.querySelector('#rb-alloc'), '50');
    change(el.querySelector('#rb-sl'), '5');
    change(el.querySelector('#rb-tickers'), 'aapl.us, msft.us');
    (el.querySelector('.chip input[type=checkbox]:not(:checked)') as HTMLInputElement).click();
    fixture.detectChanges();

    const s = spec();
    expect(s.indicators[0]).toEqual({ id: 'rsi7', kind: 'rsi', period: 7 });
    expect(s.rank).toEqual({ by: 'rsi7', order: 'desc' });
    expect((s.entry as GroupNode).conditions[0]).toEqual({
      type: 'compare',
      left: { type: 'indicator', id: 'rsi7' },
      op: '<',
      right: { type: 'constant', value: 30 },
    });
    expect(s.sizing.allocation).toBe(0.5);
    expect(s.risk.stop_loss_pct).toBe(0.05);
    expect(s.universe).toEqual({
      asset_classes: ['equity', 'crypto'],
      tickers: ['AAPL.US', 'MSFT.US'],
    });
  });

  it('adds and removes indicators', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC));
    change(el.querySelector('#rb-new-kind'), 'sma');
    button('Add indicator').click();
    fixture.detectChanges();
    expect(spec().indicators.at(-1)).toEqual({ id: 'sma20', kind: 'sma', period: 20 });

    button('Remove indicator sma50').click();
    fixture.detectChanges();
    expect(spec().indicators.map((i) => i.id)).toEqual(['rsi14', 'close', 'sma20']);
  });

  it('edits condition groups: add, switch to any, negate, remove', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC));
    const entry = () => spec().entry as GroupNode;
    const entryTree = () =>
      el.querySelector('app-condition-editor[data-node="entry"]') as HTMLElement;

    button('+ Comparison', entryTree()).click();
    fixture.detectChanges();
    expect(entry().conditions).toHaveLength(3);
    expect(entry().conditions[2]).toEqual({
      type: 'compare',
      left: { type: 'indicator', id: 'rsi14' },
      op: '>',
      right: { type: 'indicator', id: 'sma50' },
    });

    change(entryTree().querySelector('.group-type'), 'any');
    expect(entry().type).toBe('any');

    button('+ Group', entryTree()).click();
    fixture.detectChanges();
    expect(entry().conditions[3]).toMatchObject({ type: 'any', conditions: [{ type: 'compare' }] });

    // Negate the first comparison, then make its right side a number.
    const first = entryTree().querySelector('.children > li') as HTMLElement;
    button('Not', first).click();
    fixture.detectChanges();
    expect(entry().conditions[0]).toMatchObject({ type: 'not', condition: { type: 'compare' } });

    button('Remove comparison', entryTree().querySelectorAll('.children > li')[1]).click();
    fixture.detectChanges();
    expect(entry().conditions).toHaveLength(3);
    expect(entry().conditions[1]).toMatchObject({ left: { id: 'rsi14' }, op: '>' });
  });

  it('switches an operand between an indicator and a number', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC));
    const exitTree = el.querySelector('app-condition-editor[data-node="exit"]') as HTMLElement;
    change(exitTree.querySelector('select[aria-label="Right side"]'), 'i:sma50');
    expect(spec().exit).toMatchObject({ right: { type: 'indicator', id: 'sma50' } });

    const tree = el.querySelector('app-condition-editor[data-node="exit"]') as HTMLElement;
    change(tree.querySelector('select[aria-label="Left side"]'), 'const');
    change(tree.querySelector('input[aria-label="Left number"]'), '1.5');
    expect(spec().exit).toMatchObject({ left: { type: 'constant', value: 1.5 } });
  });

  it('turns a root comparison into a group and the exit rule off', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC));
    const exitTree = el.querySelector('app-condition-editor[data-node="exit"]') as HTMLElement;
    button('Add condition', exitTree).click();
    fixture.detectChanges();
    expect(spec().exit).toMatchObject({ type: 'all' });
    expect((spec().exit as GroupNode).conditions).toHaveLength(2);

    const toggle = [...el.querySelectorAll('label.check')].find((l) =>
      l.textContent?.includes('Use an exit rule'),
    );
    (toggle?.querySelector('input') as HTMLInputElement).click();
    fixture.detectChanges();
    expect(spec().exit).toBeNull();
  });

  it('shows validation errors at the exact field', () => {
    setup(toDoc(RSI_TEMPLATE_SPEC), {
      'indicators[1].period': ['Input should be less than or equal to 1000'],
      'entry.conditions[1].right.id': ["unknown indicator 'sma5'"],
      'rank.by': ["unknown indicator 'x'"],
      'sizing.allocation': ['Input should be greater than 0'],
    });

    const period = el.querySelector('#rb-ind-period-1') as HTMLInputElement;
    expect(period.getAttribute('aria-invalid')).toBe('true');
    expect(period.closest('.indicator')?.textContent).toContain('less than or equal to 1000');
    expect(el.querySelector('#rb-ind-period-0')?.getAttribute('aria-invalid')).toBe('false');

    const second = el.querySelector(
      'app-condition-editor[data-node="entry.conditions[1]"]',
    ) as HTMLElement;
    expect(
      second.querySelector('select[aria-label="Right side"]')?.getAttribute('aria-invalid'),
    ).toBe('true');
    expect(second.textContent).toContain("unknown indicator 'sma5'");
    const first = el.querySelector(
      'app-condition-editor[data-node="entry.conditions[0]"]',
    ) as HTMLElement;
    expect(first.textContent).not.toContain('unknown indicator');

    expect(el.querySelector('#rb-rank-by')?.getAttribute('aria-invalid')).toBe('true');
    expect(el.querySelector('#rb-alloc')?.getAttribute('aria-invalid')).toBe('true');
    expect(el.querySelector('#rb-alloc')?.closest('.field')?.textContent).toContain(
      'greater than 0',
    );
  });
});
