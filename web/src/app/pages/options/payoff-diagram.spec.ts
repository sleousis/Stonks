import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { OptionPayoffView } from '../../api/models';
import { PAYOFF } from './options-test-fixtures';
import { PayoffDiagram } from './payoff-diagram';

describe('PayoffDiagram', () => {
  let fixture: ComponentFixture<PayoffDiagram>;
  let el: HTMLElement;

  function render(view: OptionPayoffView): void {
    TestBed.configureTestingModule({ imports: [PayoffDiagram] });
    fixture = TestBed.createComponent(PayoffDiagram);
    fixture.componentRef.setInput('payoff', view);
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  it('shows the figures, the drawing and the legs', () => {
    render(PAYOFF);
    const figures = el.querySelector('.figures')!.textContent!;
    expect(figures).toContain('Debit $200.00');
    expect(figures).toContain('$800.00');
    expect(figures).toContain('102');
    const svg = el.querySelector('svg')!;
    expect(svg.getAttribute('role')).toBe('img');
    expect(svg.getAttribute('aria-label')).toBe(
      'Bull call spread on AAPL.US: Debit $200.00, max loss $200.00, max gain $800.00, breakeven 102.',
    );
    expect(el.querySelector('path.line')!.getAttribute('d')).toMatch(/^M/);
    expect(el.querySelector('line.spot')).not.toBeNull();
    const legs = [...el.querySelectorAll('.legs li')].map((li) => li.textContent!.trim());
    expect(legs[0]).toContain('Buy 1 call 100');
    expect(legs[1]).toContain('Sell 1 call 110');
    expect(legs[1]).toContain('$1.00');
  });

  it('says unlimited and none where a bound or breakeven is missing', () => {
    render({ ...PAYOFF, max_gain: null, breakevens: [], cost: -120 });
    const figures = el.querySelector('.figures')!.textContent!;
    expect(figures).toContain('Credit $120.00');
    expect(figures).toContain('Unlimited');
    expect(figures).toContain('None');
  });
});
