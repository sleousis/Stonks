import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { PnlSeries, PortfolioView } from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page, tick } from '../../../testing/http';
import { book } from '../../../testing/portfolio-fixtures';
import { PortfolioCard, sessionLabel } from './portfolio-card';

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

describe('PortfolioCard', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(books: PortfolioRef[], day = new Date().toISOString().slice(0, 10)) {
    const fixture = TestBed.createComponent(PortfolioCard);
    fixture.detectChanges();
    (await nextRequest(http, '/api/portfolios')).flush(page(books));
    if (books.length) {
      (await nextRequest(http, '/api/portfolio')).flush(PORTFOLIO);
      (await nextRequest(http, '/api/pnl')).flush(pnl(day));
    }
    for (let i = 0; i < 3; i++) {
      await tick(2);
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('wears the brass frame and LIVE stamp only for a live portfolio', async () => {
    const el = await render([
      book({ id: 'pf_1', name: 'Real', is_default: true, trading: 'live' }),
    ]);
    expect(el.querySelector('section')?.classList).toContain('live-frame');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('LIVE');
  });

  it('stays plain paper grey for a paper portfolio', async () => {
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })]);
    expect(el.querySelector('section')?.classList).not.toContain('live-frame');
    expect(el.querySelector('app-mode-stamp')?.textContent).toContain('PAPER');
  });

  it('links Details to Insights', async () => {
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })]);
    const details = [...el.querySelectorAll('a')].find((a) => a.textContent?.trim() === 'Details');
    expect(details?.getAttribute('href')).toBe('/insights');
  });

  it('labels a change from an earlier session with its weekday, not Today (UX-34)', async () => {
    const threeDaysAgo = new Date(Date.now() - 3 * 86_400_000).toISOString().slice(0, 10);
    const el = await render([book({ id: 'pf_1', name: 'Paper', is_default: true })], threeDaysAgo);
    const labels = [...el.querySelectorAll('app-stat-tile')].map((t) => t.textContent ?? '');
    expect(labels[1]).toContain('+$25.00');
    expect(labels[1]).not.toContain('Today');
    expect(labels[1]).toMatch(/day/);
  });

  it('with no portfolio offers to open one and hides Details (UX-13)', async () => {
    const el = await render([]);
    const links = [...el.querySelectorAll('a')];
    expect(links.map((a) => a.textContent?.trim())).toEqual(['Open a paper portfolio']);
    expect(links[0].getAttribute('href')).toBe('/welcome?step=portfolio');
    expect(el.textContent).not.toMatch(/admin/i);
    http.expectNone('/api/portfolio');
  });
});
