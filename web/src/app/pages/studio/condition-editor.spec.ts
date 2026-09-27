import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { ConditionEditor } from './condition-editor';
import {
  type ConditionNode,
  type GroupNode,
  compare,
  constantOperand,
  indicatorOperand,
} from './rule-spec';

const RSI_BELOW_30 = compare(indicatorOperand('rsi'), '<', constantOperand(30));
const CLOSE_ABOVE_SMA = compare(indicatorOperand('close'), '>', indicatorOperand('sma'));

describe('ConditionEditor', () => {
  let fixture: ComponentFixture<ConditionEditor>;
  let el: HTMLElement;
  let changes: ConditionNode[];
  let removed: number;

  function create(node: ConditionNode, removable = false, issues = {}): void {
    changes = [];
    removed = 0;
    TestBed.configureTestingModule({ imports: [ConditionEditor] });
    fixture = TestBed.createComponent(ConditionEditor);
    fixture.componentRef.setInput('node', node);
    fixture.componentRef.setInput('path', 'entry');
    fixture.componentRef.setInput('indicatorIds', ['rsi', 'close', 'sma']);
    fixture.componentRef.setInput('removable', removable);
    fixture.componentRef.setInput('issues', issues);
    fixture.componentInstance.nodeChange.subscribe((n) => changes.push(n));
    fixture.componentInstance.remove.subscribe(() => removed++);
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  function select(label: string, value: string, root: ParentNode = el): void {
    const s = root.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
    s.value = value;
    s.dispatchEvent(new Event('change'));
  }

  function button(text: string, root: ParentNode = el): HTMLButtonElement {
    const b = [...root.querySelectorAll<HTMLButtonElement>('button')].find(
      (x) => x.textContent!.trim() === text,
    );
    if (!b) throw new Error(`no button ${text}`);
    return b;
  }

  it('shows a comparison and emits a new node for each edit', () => {
    create(RSI_BELOW_30);
    expect(el.querySelector<HTMLInputElement>('input[aria-label="Right number"]')!.value).toBe(
      '30',
    );
    select('Comparison', 'crosses_above');
    expect(changes.at(-1)).toEqual({ ...RSI_BELOW_30, op: 'crosses_above' });

    select('Right side', 'i:sma');
    expect(changes.at(-1)).toEqual({ ...RSI_BELOW_30, right: indicatorOperand('sma') });

    const num = el.querySelector<HTMLInputElement>('input[aria-label="Right number"]')!;
    num.value = '';
    num.dispatchEvent(new Event('change'));
    // An empty box stays 0 so no NaN reaches the JSON.
    expect(changes.at(-1)).toEqual({ ...RSI_BELOW_30, right: constantOperand(0) });
  });

  it('turns the root comparison into a group to add a second condition', () => {
    create(RSI_BELOW_30);
    expect(el.querySelector('button[aria-label="Remove comparison"]')).toBeNull();
    button('Add condition').click();
    const g = changes.at(-1) as GroupNode;
    expect(g.type).toBe('all');
    expect(g.conditions).toHaveLength(2);
    expect(g.conditions[0]).toEqual(RSI_BELOW_30);
  });

  it('wraps a node in Not', () => {
    create(RSI_BELOW_30);
    button('Not').click();
    expect(changes.at(-1)).toEqual({ type: 'not', condition: RSI_BELOW_30 });
  });

  it('unwraps a Not', () => {
    create({ type: 'not', condition: RSI_BELOW_30 });
    expect(el.textContent).toContain('true when the condition below is false');
    button('Remove not').click();
    expect(changes.at(-1)).toEqual(RSI_BELOW_30);
  });

  it('edits a group: match type, children, and adding comparisons', () => {
    const group: GroupNode = { type: 'all', conditions: [RSI_BELOW_30, CLOSE_ABOVE_SMA] };
    create(group);
    select('Group match', 'any');
    expect(changes.at(-1)).toEqual({ ...group, type: 'any' });

    button('+ Comparison').click();
    expect((changes.at(-1) as GroupNode).conditions).toHaveLength(3);

    // The second child's paths nest under the group's.
    const second = el.querySelectorAll('app-condition-editor')[1];
    expect(second.getAttribute('data-node')).toBe('entry.conditions[1]');
    select('Comparison', '>=', second);
    expect(changes.at(-1)).toEqual({
      ...group,
      conditions: [RSI_BELOW_30, { ...CLOSE_ABOVE_SMA, op: '>=' }],
    });
  });

  it('removing the last child of a root group leaves a fresh comparison', () => {
    create({ type: 'any', conditions: [RSI_BELOW_30] });
    el.querySelector<HTMLButtonElement>('button[aria-label="Remove comparison"]')!.click();
    expect(changes.at(-1)).toEqual(compare(indicatorOperand('rsi'), '>', indicatorOperand('close')));
    expect(removed).toBe(0);
  });

  it('a removable group leaves with its last child', () => {
    create({ type: 'any', conditions: [RSI_BELOW_30] }, true);
    el.querySelector<HTMLButtonElement>('button[aria-label="Remove comparison"]')!.click();
    expect(removed).toBe(1);
  });

  it('shows the issues for its own fields', () => {
    create(RSI_BELOW_30, false, {
      'entry.right.value': ['Input should be a finite number'],
      'exit.op': ['not mine'],
    });
    const alerts = [...el.querySelectorAll('.issue')].map((p) => p.textContent!.trim());
    expect(alerts).toEqual(['Input should be a finite number']);
  });
});
