import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { RiskAdjustmentView } from '../../api/models';
import { ApiError, toApiError } from '../../core/http/api-error';
import {
  OrderRefusalPanel,
  adjustmentText,
  allowedQuantity,
  plainRefusal,
  refusalOf,
  ruleLabel,
  ruleReason,
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

describe('plainRefusal (PO-03)', () => {
  const ctx = { portfolio: "Jamie's paper portfolio", currency: 'USD' };

  it('says a halt in words, names the portfolio and asks for a resume', () => {
    const r = plainRefusal(
      'trading is halted for this portfolio: kill (portfolio pf_c5e4a4fc1dd5): Stopped from the console.',
      [],
      ctx,
    );
    expect(r.message).toBe(
      "Trading is stopped for Jamie's paper portfolio. Resume it to place orders.",
    );
    expect(r.halted).toBe(true);
    expect(r.message).not.toContain('pf_');
  });

  it('says not enough cash instead of the cash buffer room', () => {
    const r = plainRefusal(
      'the risk rules refuse this order: quantity 10.0 exceeds cash_buffer room 0.0',
      [adj({ rule: 'cash_buffer', original_quantity: 10, adjusted_quantity: 0 })],
      ctx,
    );
    expect(r.message).toBe(
      'Not enough cash. You have $0.00 free, so this buy cannot go through. ' +
        'Sell something first or lower the quantity.',
    );
    expect(r.halted).toBe(false);
  });

  it('writes a smaller allowed order and a wrong-side stop in words', () => {
    expect(plainRefusal('the risk rules allow 4.0 of the 10.0 asked for: x', [], ctx).message).toBe(
      'The risk limits allow 4 of the 10 you asked for.',
    );
    expect(plainRefusal('the stop of a buy must be below the entry 187', [], ctx).message).toBe(
      'For a buy, the stop goes below the price.',
    );
    expect(
      plainRefusal('the stop of a short sale must be above the entry 5', [], ctx).message,
    ).toBe('For a sale, the stop goes above the price.');
  });

  it('tidies anything else: capital first letter, no ids, no 10.0', () => {
    expect(plainRefusal('no close for 10.0 shares (portfolio pf_1)', [], ctx).message).toBe(
      'No close for 10 shares.',
    );
  });

  it('hides the engine reason for a room cut and keeps a readable one', () => {
    expect(ruleReason(adj({ reason: 'quantity 10.0 exceeds cash_buffer room 0.0' }))).toBeNull();
    expect(ruleReason(adj({ reason: 'caps AAA.US at 10% of the book' }))).toBe(
      'Caps AAA.US at 10% of the book.',
    );
  });
});

describe('refusal words', () => {
  it('names a rule and what it did in plain words', () => {
    expect(ruleLabel('max_position_weight')).toBe('Largest position');
    expect(ruleLabel('cash_buffer')).toBe('Cash on hand');
    expect(ruleLabel('sector_cap')).toBe('Sector cap');
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
    expect(el.querySelector('.rules li')?.textContent).toContain('Largest position');
    expect(el.querySelector('.rules li')?.textContent).toContain('Cuts 100 to 40');
    const box = el.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(box.parentElement?.textContent).toContain('Accept a smaller order of 40');
    box.click();
    expect(fixture.componentInstance.allowReduce()).toBe(true);
  });

  it('links a halted order to the page that resumes trading', () => {
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const fixture = TestBed.createComponent(OrderRefusalPanel);
    fixture.componentRef.setInput('refusal', {
      message: 'Trading is stopped for Book. Resume it to place orders.',
      adjustments: [],
      allowed: null,
      halted: true,
    });
    fixture.detectChanges();
    const link = (fixture.nativeElement as HTMLElement).querySelector('a')!;
    expect(link.getAttribute('href')).toBe('/ops/halts');
    expect(link.textContent).toContain('Resume trading');
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
