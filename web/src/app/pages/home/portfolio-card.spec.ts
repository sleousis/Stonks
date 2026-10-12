import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PnlSeries, PortfolioView } from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import type { SubscriptionView } from '../../api/subscriptions.service';
import { HomeStore } from './home-store';
import { PortfolioCard, inRange, sessionLabel, splitMoney } from './portfolio-card';

const PORTFOLIO: PortfolioView = {
  taken_at: '2026-09-25T21:00:00Z',
  tick_id: 't2',
  cash: 1_000,
  positions: [],
  positions_value: 0,
  total_value: 1_000,
  snapshot_total_value: 1_000,
};

function pnl(day: string): PnlSeries {
  return {
    strategy_id: null,
    rows: [
      {
        day,
        total_value: 1_000,
        daily_change: 25,
        daily_return: 0.025,
        cumulative_return: 0.025,
        drawdown: 0,
      },
    ],
  };
}

describe('sessionLabel (UX-34)', () => {
  const now = new Date('2026-09-28T15:00:00Z'); // a Monday

  it('says Today for today, the weekday for earlier this week, else Last session', () => {
    expect(sessionLabel('2026-09-28', now)).toBe('Today');
    expect(sessionLabel('2026-09-25', now)).toMatch(/Friday/);
    expect(sessionLabel('2026-09-10', now)).toBe('Last session');
    expect(sessionLabel(null, now)).toBe('Today');
  });
});

describe('overview helpers', () => {
  it('splits the cents off a money figure for the muted ink', () => {
    expect(splitMoney('$10,412.37')).toEqual(['$10,412', '.37']);
    expect(splitMoney('10.412,37 €')).toEqual(['10.412', ',37 €']);
    expect(splitMoney('•••••')).toEqual(['•••••', '']);
  });

  it('keeps the rows of the picked range', () => {
    const rows = ['2026-01-01', '2026-09-01', '2026-09-20', '2026-09-25'].map((day) => ({ day }));
    expect(inRange(rows, '1W').map((r) => r.day)).toEqual(['2026-09-20', '2026-09-25']);
    expect(inRange(rows, '1M')).toHaveLength(3);
    expect(inRange(rows, 'All')).toHaveLength(4);
  });
});

describe('PortfolioCard', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting(), HomeStore],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(
    books: PortfolioRef[],
    day = new Date().toISOString().slice(0, 10),
    extra: {
      portfolio?: Partial<PortfolioView>;
      pnl?: Partial<PnlSeries>;
      orders?: { status: string }[];
      follows?: Partial<SubscriptionView>[];
    } = {},
  ) {
    const fixture = TestBed.createComponent(PortfolioCard);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page(books));
    if (books.length) {
      (await nextRequest(http, '/api/portfolio')).flush({ ...PORTFOLIO, ...extra.portfolio });
      (await nextRequest(http, '/api/pnl')).flush({ ...pnl(day), ...extra.pnl });
      (await nextRequest(http, '/api/orders')).flush(page(extra.orders ?? []));
    }
    (await nextRequest(http, '/api/subscriptions')).flush(page(extra.follows ?? []));
    for (let i = 0; i < 3; i++) {
      await tick(2);
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the value in the base currency and the returns without deposits', async () => {
    const el = await render(
      [book({ id: 'pf_1', name: 'Main', is_default: true, base_currency: 'EUR' })],
      undefined,
      {
        portfolio: { currency: 'USD', base_currency: 'EUR', total_value_base: 920 },
        pnl: { twr: 0.051, mwr: 0.12 },
      },
    );
    expect(el.textContent).toContain('Value in EUR: €920.00');
    // One day of history: the plain return only, no yearly rate (NB-14).
    expect(el.querySelector('.returns')?.textContent).toContain(
      'Since the start: return +5.1%. Deposits',
    );
  });

  it('adds the yearly rate on your money after about three months (NB-14)', async () => {
    const long = pnl('2026-09-25');
    const rows = [{ ...long.rows[0], day: '2026-06-01' }, ...long.rows];
    const el = await render([book({ id: 'pf_1', name: 'Main', is_default: true })], undefined, {
      pnl: { rows, twr: 0.051, mwr: 0.12 },
    });
    expect(el.querySelector('.returns')?.textContent).toContain(
      'Since the start: return +5.1%, +12.0% a year on your money.',
    );
  });

  it('shows no base line when the base currency is the one shown', async () => {
    const el = await render([book({ id: 'pf_1', name: 'Main', is_default: true })]);
    expect(el.textContent).not.toContain('Value in');
    expect(el.querySelector('.returns')).toBeNull();
  });

  it('sets the figure in brass with a LIVE stamp only for a live portfolio', async () => {
    const el = await render([
      book({ id: 'pf_1', name: 'Real', is_default: true, trading: 'live' }),
    ]);
    expect(el.querySelector('.value')?.classList).toContain('brass');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('LIVE');
  });

  it('stays plain paper grey for a paper portfolio', async () => {
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })]);
    expect(el.querySelector('.value')?.classList).not.toContain('brass');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
  });

  it('stamps a read-only broker portfolio READ ONLY, never PAPER', async () => {
    const el = await render([
      book({ id: 'pf_1', name: 'DEGIRO', is_default: true, kind: 'broker', base_currency: 'EUR' }),
    ]);
    const stamp = el.querySelector('app-mode-stamp')?.textContent ?? '';
    expect(stamp).toContain('READ ONLY');
    expect(stamp).not.toContain('PAPER');
  });

  it('shows the value in the base currency when the book reports in it', async () => {
    const el = await render(
      [
        book({
          id: 'pf_1',
          name: 'DEGIRO',
          is_default: true,
          kind: 'broker',
          base_currency: 'EUR',
        }),
      ],
      undefined,
      {
        portfolio: {
          currency: 'EUR',
          base_currency: 'EUR',
          total_value: 1_000,
          total_value_base: 950,
        },
      },
    );
    const value = el.querySelector('.value')?.textContent ?? '';
    expect(value).toContain('€950.00');
    expect(value).not.toContain('$');
  });

  it('links Details to Insights', async () => {
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })]);
    const details = [...el.querySelectorAll('a')].find((a) => a.textContent?.trim() === 'Details');
    expect(details?.getAttribute('href')).toBe('/insights');
  });

  it('labels a change from an earlier session with its weekday, not Today (UX-34)', async () => {
    const threeDaysAgo = new Date(Date.now() - 3 * 86_400_000).toISOString().slice(0, 10);
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })], threeDaysAgo);
    const change = el.querySelector('.change')?.textContent ?? '';
    expect(change).toContain('+$25.00');
    expect(change).toContain('+2.50%');
    expect(change).not.toContain('today');
    expect(change).toMatch(/on \w+day/);
  });

  it('with no portfolio offers to open one and hides Details (UX-13)', async () => {
    const el = await render([]);
    const links = [...el.querySelectorAll('a')];
    expect(links.map((a) => a.textContent?.trim())).toEqual(['Open a paper portfolio']);
    expect(links[0].getAttribute('href')).toBe('/welcome?step=portfolio');
    expect(el.textContent).not.toMatch(/admin/i);
    http.expectNone('/api/portfolio');
  });

  it('lists cash, invested, open orders and the worst drop beside the figure', async () => {
    const rows = [
      { ...pnl('2026-09-24').rows[0], drawdown: -0.0231 },
      { ...pnl('2026-09-25').rows[0], drawdown: -0.01 },
    ];
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })], undefined, {
      portfolio: { cash: 3_108.52, positions_value: 7_303.85 },
      pnl: { rows },
      orders: [{ status: 'submitted' }, { status: 'filled' }, { status: 'pending' }],
    });
    const facts = [...el.querySelectorAll('.fact-list > div')].map((d) => [
      d.querySelector('dt')?.textContent?.trim(),
      d.querySelector('dd')?.textContent?.trim(),
    ]);
    expect(facts).toEqual([
      ['Cash', '$3,108.52'],
      ['Invested', '$7,303.85'],
      ['Open orders', '2'],
      ['Worst drop', '-2.31%'],
    ]);
    expect(el.querySelector('.fact-list dd.loss')?.textContent).toContain('-2.31%');
  });

  it('draws the value curve with a range picker once there are two days', async () => {
    const rows = [pnl('2026-09-24').rows[0], { ...pnl('2026-09-25').rows[0], total_value: 1_100 }];
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })], undefined, {
      pnl: { rows },
    });
    expect(el.querySelector('app-equity-area [role="img"]')?.getAttribute('aria-label')).toBe(
      'Portfolio value over 1M: from $1,000.00 to $1,100.00.',
    );
    const ranges = [...el.querySelectorAll('.range [role="radio"]')].map((b) =>
      b.textContent?.trim(),
    );
    expect(ranges).toEqual(['1W', '1M', '3M', '1Y', 'All']);
  });

  it('counts paper days toward a broker account on the meter', async () => {
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })], undefined, {
      follows: [
        { id: 's1', portfolio_id: 'pf_1', paper_days_completed: 6, paper_days_required: 20 },
        { id: 's2', portfolio_id: 'pf_1', paper_days_completed: 2, paper_days_required: 20 },
      ],
    });
    expect(el.querySelector('.meter-head')?.textContent).toContain('6 of 20');
    expect(el.querySelectorAll('.ticks li')).toHaveLength(20);
    expect(el.querySelectorAll('.ticks li.on')).toHaveLength(6);
    expect(el.querySelector('.meter-text')?.textContent).toContain('After 20 paper days');
  });
});
