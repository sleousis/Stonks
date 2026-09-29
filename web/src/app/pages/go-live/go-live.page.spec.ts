import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { BrokerInfo, LeaderboardRow, LeaderboardView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { tick } from '../../../testing/http';
import { paper } from '../../../testing/strategy-fixtures';
import { GoLivePage, reviewOrder } from './go-live.page';

function row(id: string, extra: Partial<LeaderboardRow> = {}): LeaderboardRow {
  return {
    rank: 1,
    strategy_id: id,
    strategy_name: null,
    class_path: 'stonks.strategies.examples.buy_and_hold:BuyAndHold',
    status: 'shadow',
    paper: paper(),
    book_trades: 0,
    live_since: null,
    survival_passed: 4,
    survival_total: 4,
    golive_passed: false,
    ...extra,
  };
}

const BOARD: LeaderboardView = {
  sort: 'return',
  as_of: '2026-09-25',
  rows: [
    row('momentum_0a1b2c3d', { status: 'active', golive_passed: true }),
    row('value_1a2b3c4d'),
    row('trend_2b3c4d5e', { golive_passed: true }),
  ],
};

const PAPER_BROKER: BrokerInfo = {
  kind: 'simulated',
  paper: true,
  allow_live: false,
  credentials_configured: false,
};

describe('GoLivePage (Strategy review)', () => {
  let fixture: ComponentFixture<GoLivePage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [GoLivePage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(GoLivePage);
    el = fixture.nativeElement;
  });

  afterEach(() => controller.verify());

  async function flushAll(board = BOARD, broker = PAPER_BROKER): Promise<void> {
    for (let i = 0; i < 4; i++) {
      for (const req of controller.match(() => true)) {
        const path = req.request.url.split('?')[0];
        if (path === '/api/strategies/leaderboard') req.flush(board);
        else if (path === '/api/brokers') req.flush(broker);
        else throw new Error(`unexpected request ${path}`);
      }
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('is called Strategy review and lists the ready ones first (F54)', async () => {
    fixture.detectChanges();
    await flushAll();
    expect(el.querySelector('h1')?.textContent).toContain('Strategy review');
    expect(el.textContent).toContain('1 ready to approve, 2 on trial');
    const names = [...el.querySelectorAll('a.name .id')].map((a) => a.textContent?.trim());
    expect(names).toEqual(['Trend 2b3c', 'Value 1a2b', 'Momentum 0a1b']);
    // Kind names, never class names (M6).
    expect(el.textContent).toContain('Buy and hold');
    expect(el.textContent).not.toContain('BuyAndHold');
  });

  it('sends each row to the Review tab of its strategy page (F27)', async () => {
    fixture.detectChanges();
    await flushAll();
    const links = [...el.querySelectorAll<HTMLAnchorElement>('a.review')].map((a) =>
      a.getAttribute('href'),
    );
    expect(links[0]).toBe('/strategies/trend_2b3c4d5e?tab=review');
    expect(el.querySelectorAll('app-strategy-verdict').length).toBe(3);
    expect(el.textContent).toContain('Worth following');
  });

  it('says a paper broker moves no real money and points risk limits to Settings', async () => {
    fixture.detectChanges();
    await flushAll();
    const broker = el.querySelector('section[aria-labelledby="broker-title"]')!;
    expect(broker.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
    expect(broker.textContent).toContain('Simulated, paper money');
    expect(broker.textContent).toContain('moves no real money');
    expect(el.querySelector('a[href="/settings"]')).not.toBeNull();
  });

  it('uses trader words only (UX-09, B1)', async () => {
    fixture.detectChanges();
    await flushAll();
    expect(el.textContent).not.toMatch(/shadow|promot|regist|(?<!go-)\blive\b/i);
  });

  it('opens the Review tab for an old ?strategy= link', async () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    fixture.componentRef.setInput('strategy', 'value_1a2b3c4d');
    fixture.detectChanges();
    await flushAll();
    expect(navigate).toHaveBeenCalledWith(['/strategies', 'value_1a2b3c4d'], {
      queryParams: { tab: 'review' },
      replaceUrl: true,
    });
  });

  it('points to the Lab when nothing is on trial', async () => {
    fixture.detectChanges();
    await flushAll({ ...BOARD, rows: [] });
    expect(el.textContent).toContain('No strategies yet');
  });

  it('orders the queue: ready, then on trial, then approved', () => {
    expect(reviewOrder(BOARD.rows).map((r) => r.strategy_id)).toEqual([
      'trend_2b3c4d5e',
      'value_1a2b3c4d',
      'momentum_0a1b2c3d',
    ]);
  });
});
