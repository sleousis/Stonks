import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { BreadthCard, aboveText } from './breadth-card';

const BREADTH = {
  universe: 'every stock in the lake',
  breadth: {
    as_of: '2026-09-25',
    members: 500,
    advancers: 320,
    decliners: 160,
    unchanged: 20,
    advance_decline_ratio: 2,
    above_50: { days: 50, count: 300, eligible: 500, pct: 0.6 },
    above_200: { days: 200, count: 250, eligible: 480, pct: 250 / 480 },
    new_highs: 25,
    new_lows: 4,
    high_low_window: 252,
    index: 'SPY.US',
    distribution_days: 3,
    distribution_dates: ['2026-09-10', '2026-09-17', '2026-09-23'],
    distribution_window: 25,
    lines: [
      {
        key: 'advance_decline',
        text: 'More stocks rose than fell: 320 up, 160 down.',
        tone: 'good',
      },
      {
        key: 'distribution',
        text: '3 distribution days on SPY in the last 25 sessions: some selling pressure.',
        tone: 'neutral',
      },
    ],
  },
};

describe('BreadthCard', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(body: object | null, status?: number) {
    const fixture = TestBed.createComponent(BreadthCard);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/market/breadth');
    if (status) req.flush({ detail: 'down' }, { status, statusText: 'Server error' });
    else req.flush(body);
    await tick(2);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('shows the numbers with plain words', async () => {
    const { el } = await render(BREADTH);
    const text = el.textContent ?? '';
    expect(el.querySelector('h2')?.textContent).toContain('Market breadth');
    expect(text).toContain('320 up, 160 down');
    expect(text).toContain('60% (300 of 500)');
    expect(text).toContain('25 highs, 4 lows');
    expect(text).toContain('Distribution days on SPY');
    expect(text).toContain('3 in 25 sessions');
    expect(text).toContain('More stocks rose than fell');
    expect(text).toContain('nothing trades on it');
    const lines = el.querySelectorAll('ul[aria-label="What the numbers mean"] li');
    expect(lines.length).toBe(2);
    expect(lines[0].classList).toContain('good');
  });

  it('hides the tiles and explains an empty lake', async () => {
    const empty = {
      ...BREADTH,
      breadth: {
        ...BREADTH.breadth,
        as_of: null,
        distribution_days: null,
        lines: [{ key: 'empty', text: 'No price data yet.', tone: 'neutral' }],
      },
    };
    const { el } = await render(empty);
    expect(el.querySelector('app-stat-tile')).toBeNull();
    expect(el.textContent).toContain('No price data yet.');
  });

  it('offers a retry when it fails', async () => {
    const { fixture, el } = await render(null, 500);
    expect(el.textContent).toContain('Could not load breadth');
    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Try again'))!.click();
    (await nextRequest(http, '/api/market/breadth')).flush(BREADTH);
    await tick(2);
    fixture.detectChanges();
    expect(el.textContent).toContain('320 up');
  });

  it('says when a stock has no average yet', () => {
    expect(aboveText({ days: 200, count: 0, eligible: 0, pct: null })).toBe('Not enough history');
  });
});
