import { TestBed } from '@angular/core/testing';

import type { RiskAdjustmentView } from '../../api/models';
import { ApiError, toApiError } from '../../core/http/api-error';
import {
  OrderRefusalPanel,
  adjustmentText,
  allowedQuantity,
  refusalOf,
  ruleLabel,
} from './order-refusal';

function adj(over: Partial<RiskAdjustmentView> = {}): RiskAdjustmentView {
  return {
    rule: 'max_position_weight',
    ticker: 'AAA.US',
    side: 'buy',
    original_quantity: 100,
    adjusted_quantity: 40,
    reason: 'caps AAA.US at 10% of the book',
    ...over,
  };
}

function refusedError(adjustments: RiskAdjustmentView[]): ApiError {
  return toApiError(
    {
      title: 'Conflict',
      status: 409,
      code: 'order_refused',
      detail: 'the risk rules allow 40 of the 100 asked for',
      risk_adjustments: adjustments,
    },
    { status: 409 },
  );
}

describe('refusalOf', () => {
  it('reads the adjustments and the smaller quantity the rules allow', () => {
    const r = refusalOf(refusedError([adj(), adj({ rule: 'liquidity', adjusted_quantity: 60 })]));
    expect(r?.message).toContain('allow 40');
    expect(r?.adjustments).toHaveLength(2);
    expect(r?.allowed).toBe(40);
  });

  it('offers nothing smaller when a rule drops the order', () => {
    expect(refusalOf(refusedError([adj({ adjusted_quantity: 0 })]))?.allowed).toBeNull();
    expect(refusalOf(refusedError([]))?.allowed).toBeNull();
  });

  it('ignores every other failure', () => {
    expect(refusalOf(new ApiError(409, 'Conflict', 'busy', [], 'conflict'))).toBeNull();
    expect(refusalOf(new Error('x'))).toBeNull();
  });
});

describe('refusal words', () => {
  it('names a rule and what it did in plain words', () => {
    expect(ruleLabel('max_position_weight')).toBe('Max position weight');
    expect(ruleLabel('')).toBe('A risk rule');
    expect(adjustmentText(adj())).toBe('Cuts 100 to 40');
    expect(adjustmentText(adj({ adjusted_quantity: 0 }))).toBe('Drops the order');
    expect(allowedQuantity([adj({ adjusted_quantity: 100 })])).toBeNull();
  });
});

describe('OrderRefusalPanel', () => {
  it('lists each rule and offers the smaller order', () => {
    const fixture = TestBed.createComponent(OrderRefusalPanel);
    const refusal = refusalOf(refusedError([adj()]))!;
    fixture.componentRef.setInput('refusal', refusal);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Not placed');
    expect(el.querySelector('.rules li')?.textContent).toContain('Max position weight');
    expect(el.querySelector('.rules li')?.textContent).toContain('Cuts 100 to 40');
    const box = el.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(box.parentElement?.textContent).toContain('Accept a smaller order of 40');
    box.click();
    expect(fixture.componentInstance.allowReduce()).toBe(true);
  });

  it('has no choice when nothing smaller would pass', () => {
    const fixture = TestBed.createComponent(OrderRefusalPanel);
    fixture.componentRef.setInput(
      'refusal',
      refusalOf(refusedError([adj({ adjusted_quantity: 0 })])),
    );
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('input')).toBeNull();
  });
});
