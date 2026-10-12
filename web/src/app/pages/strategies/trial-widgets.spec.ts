import type { Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { DaysRing, TRIAL_DAYS_TARGET } from './days-ring';
import { Sparkline, sparkPath } from './sparkline';
import { METER_TICKS, TrialMeter } from './trial-meter';
import {
  bestReturn,
  followingByStrategy,
  strategiesStandfirst,
  worstDrop,
  type StrategyRow,
} from './strategies.page';
import type { SubscriptionView } from '../../api/subscriptions.service';

function render<T>(cmp: Type<T>, inputs: Record<string, unknown>): HTMLElement {
  const fixture = TestBed.createComponent(cmp);
  for (const [k, v] of Object.entries(inputs)) fixture.componentRef.setInput(k, v);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('trial widgets', () => {
  it('draws a sparkline up or down by its first and last value', () => {
    expect(sparkPath([1])).toBeNull();
    expect(sparkPath([1, 2, 3])?.tone).toBe('up');
    expect(sparkPath([3, 2, 1])?.tone).toBe('down');
    const el = render(Sparkline, { values: [10, 12, 11] });
    expect(el.getAttribute('data-tone')).toBe('up');
    expect(el.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
    expect(render(Sparkline, { values: [] }).querySelector('.none')).not.toBeNull();
  });

  it('shows the days on trial beside a ring, a dash without any', () => {
    expect(render(DaysRing, { days: 23 }).textContent?.trim()).toBe('23');
    expect(render(DaysRing, { days: TRIAL_DAYS_TARGET + 5 }).classList).toContain('full');
    expect(render(DaysRing, { days: null }).textContent?.trim()).toBe('–');
  });

  it('lights a share of 20 ticks and says the days in words', () => {
    const el = render(TrialMeter, { days: 6, target: 20 });
    expect(el.querySelectorAll('.tick').length).toBe(METER_TICKS);
    expect(el.querySelectorAll('.tick.on').length).toBe(6);
    expect(el.textContent).toContain('6 of 20');
  });
});

function row(id: string, status: StrategyRow['status'], ret: number | null, dd: number | null) {
  return {
    id,
    name: id,
    status,
    paper_return: ret,
    max_drawdown: dd,
  } as StrategyRow;
}

describe('strategies summary', () => {
  const rows = [
    row('quant', 'active', 0.0842, -0.031),
    row('tsmom', 'active', -0.0314, -0.068),
    row('ath', 'shadow', -0.0042, -0.0127),
    row('old', 'retired', 0.5, -0.5),
  ];

  it('picks the best return and worst drop, leaving retired ones out', () => {
    expect(bestReturn(rows)?.id).toBe('quant');
    expect(worstDrop(rows)?.id).toBe('tsmom');
  });

  it('builds the standfirst from the counts and the leader', () => {
    expect(strategiesStandfirst(rows)).toBe('2 approved, 1 on trial. quant leads at +8.42%.');
    expect(strategiesStandfirst([])).toContain('No strategies yet');
  });

  it('prefers the picked portfolio and stamps real money only at a live portfolio', () => {
    const sub = (over: Partial<SubscriptionView>) =>
      ({ enabled: true, ...over }) as SubscriptionView;
    const map = followingByStrategy(
      [
        sub({ strategy_id: 'a', portfolio_id: 'pf_other', mode: 'notify' }),
        sub({ strategy_id: 'a', portfolio_id: 'pf_live', mode: 'auto' }),
        sub({ strategy_id: 'b', portfolio_id: 'pf_other', mode: 'paper' }),
        sub({ strategy_id: 'c', portfolio_id: 'pf_live', mode: 'paper', enabled: false }),
      ],
      'pf_live',
      (id) => id === 'pf_live',
    );
    expect(map.get('a')).toEqual({ words: 'Automatic', live: true });
    expect(map.get('b')).toEqual({ words: 'Paper', live: false });
    expect(map.get('c')).toEqual({ words: 'Paused', live: null });
  });
});
