import { computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { ExecutionService } from '../../api/execution.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { book } from '../../../testing/portfolio-fixtures';
import { RebalancePage, algoText, parseTargets, skipText } from './rebalance.page';

describe('rebalance helpers', () => {
  it('parses weights as fractions or percents', () => {
    expect(parseTargets('aapl.us=30%\nMSFT.US=0.2, ')).toEqual([
      { ticker: 'AAPL.US', weight: 0.3 },
      { ticker: 'MSFT.US', weight: 0.2 },
    ]);
  });

  it('names a line it cannot read', () => {
    expect(parseTargets('AAPL.US')).toBe('"AAPL.US" is not TICKER=WEIGHT');
    expect(parseTargets('AAPL.US=-1')).toBe('"AAPL.US=-1" is not TICKER=WEIGHT');
  });

  it('says why a line has no trade', () => {
    expect(skipText('below_one_share')).toBe('Less than one share');
    expect(skipText(null)).toBe('');
  });

  it('describes the algo setting in plain words', () => {
    expect(algoText(null)).toBe('Plain limit orders');
    expect(algoText({ algo: 'adaptive', params: { priority: 'patient' } })).toBe(
      'Adaptive, patient',
    );
    expect(algoText({ algo: 'vwap', params: { start_minutes: 0, end_minutes: 90 } })).toBe(
      'VWAP, 0 to 90 min after the open',
    );
  });
});

describe('RebalancePage', () => {
  function setup() {
    const current = signal(book({ id: 'pf_1', name: 'Main' }));
    const api = {
      settings: vi.fn(async () => ({ items: [] })),
      parents: vi.fn(async () => ({ items: [] })),
      plan: vi.fn(async () => ({
        portfolio_id: 'pf_1',
        turnover: 0.5,
        total_cost: 1,
        lines: [{ ticker: 'AAPL.US', side: 'buy', quantity: 1 }],
      })),
      confirm: vi.fn(async () => ({ written: 1 })),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: ExecutionService, useValue: api },
        {
          provide: PortfolioContextService,
          useValue: { current, live: computed(() => false), query: () => ({}) },
        },
      ],
    });
    vi.spyOn(TestBed.inject(ConfirmService), 'confirm').mockResolvedValue(true);
    const page = TestBed.createComponent(RebalancePage).componentInstance as unknown as {
      targetsText: string;
      preview(): Promise<void>;
      write(): Promise<void>;
      plan: () => unknown;
    };
    return { page, api, current };
  }

  it('never writes tickets for targets other than the previewed plan', async () => {
    const { page, api } = setup();
    page.targetsText = 'AAPL.US=50%';
    await page.preview();
    expect(page.plan()).not.toBeNull();
    page.targetsText = 'MSFT.US=100%';
    await page.write();
    expect(api.confirm).not.toHaveBeenCalled();
    expect(page.plan()).toBeNull();
  });

  it('never writes tickets for another portfolio than the previewed one', async () => {
    const { page, api, current } = setup();
    page.targetsText = 'AAPL.US=50%';
    await page.preview();
    current.set(book({ id: 'pf_2', name: 'Other', trading: 'live' }));
    await page.write();
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it('writes the previewed plan', async () => {
    const { page, api } = setup();
    page.targetsText = 'AAPL.US=50%';
    await page.preview();
    await page.write();
    expect(api.confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        portfolio_id: 'pf_1',
        targets: [{ ticker: 'AAPL.US', weight: 0.5 }],
      }),
    );
  });
});
