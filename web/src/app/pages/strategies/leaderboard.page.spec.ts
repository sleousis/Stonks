import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { LeaderboardRow, LeaderboardView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { paper } from '../../../testing/strategy-fixtures';
import { LeaderboardPage, goliveLabel, rowVerdict, stageLabel } from './leaderboard.page';

function row(id: string, rank: number, extra: Partial<LeaderboardRow> = {}): LeaderboardRow {
  return {
    rank,
    strategy_id: id,
    class_path: 'stonks.strategies.examples.momentum:Momentum',
    status: 'shadow',
    paper: paper(),
    book_trades: 2,
    live_since: null,
    survival_passed: 3,
    survival_total: 4,
    golive_passed: false,
    ...extra,
  };
}

const BOARD: LeaderboardView = {
  sort: 'sharpe',
  as_of: '2026-09-25',
  rows: [
    row('mom_v2', 1, { golive_passed: true }),
    row('value_v1', 2, { status: 'active', golive_passed: true }),
  ],
};

describe('leaderboard labels', () => {
  it('names the status in ladder words, never Live (B1)', () => {
    expect(stageLabel({ status: 'shadow' })).toBe('On trial');
    expect(rowVerdict(row('a', 1, { golive_passed: true, survival_passed: 4 })).label).toBe(
      'Worth following',
    );
    expect(stageLabel({ status: 'active' })).toBe('Approved');
    expect(stageLabel({ status: 'retired' })).toBe('Retired');
    expect(rowVerdict(row('b', 1)).label).toBe('Not good enough yet');
    expect(goliveLabel(true)).toBe('Passed');
    expect(goliveLabel(false)).toBe('Failed');
    expect(goliveLabel(null)).toBe('Not checked');
  });
});

describe('LeaderboardPage', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('ranks strategies and links each to its strategy page (F27)', async () => {
    const fixture = TestBed.createComponent(LeaderboardPage);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/strategies/leaderboard');
    expect(req.request.urlWithParams).toContain('sort=sharpe');
    req.flush(BOARD);
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const links = [...el.querySelectorAll('a.name')].map((a) => a.getAttribute('href'));
    expect(links).toEqual(['/strategies/mom_v2', '/strategies/value_v1']);
    expect(el.textContent).toContain('On trial');
    expect(el.textContent).toContain('Momentum');
    expect(el.textContent).not.toContain(':Momentum');
    expect(el.textContent).toContain('Approved');
    expect(el.textContent).not.toMatch(/\bLive\b/);
    expect(el.querySelectorAll('app-strategy-verdict').length).toBe(2);
    expect(el.textContent).toContain('3 of 4');
    expect(el.textContent).toContain('+3.00%');

    const select = el.querySelector<HTMLSelectElement>('select')!;
    select.value = 'drawdown';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const again = await nextRequest(http, '/api/strategies/leaderboard');
    expect(again.request.urlWithParams).toContain('sort=drawdown');
    again.flush({ ...BOARD, sort: 'drawdown' });
    await tick();
  });

  it('points to the lab when nothing is registered', async () => {
    const fixture = TestBed.createComponent(LeaderboardPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/strategies/leaderboard')).flush({
      sort: 'sharpe',
      as_of: null,
      rows: [],
    });
    await tick();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('No strategies yet');
  });
});
