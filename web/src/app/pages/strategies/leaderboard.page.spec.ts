import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { LeaderboardRow, LeaderboardView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { paper } from '../../../testing/strategy-fixtures';
import { LeaderboardPage, goliveLabel, stageLabel } from './leaderboard.page';

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
  it('names the stage from the status and the go-live verdict', () => {
    expect(stageLabel({ status: 'shadow', golive_passed: true })).toBe('Ready');
    expect(stageLabel({ status: 'shadow', golive_passed: false })).toBe('Paper');
    expect(stageLabel({ status: 'active', golive_passed: null })).toBe('Live');
    expect(stageLabel({ status: 'retired', golive_passed: null })).toBe('Stopped');
    expect(goliveLabel(true)).toBe('Passed');
    expect(goliveLabel(false)).toBe('Not yet');
    expect(goliveLabel(null)).toBe('–');
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

  it('ranks strategies and links each to its tear sheet', async () => {
    const fixture = TestBed.createComponent(LeaderboardPage);
    fixture.detectChanges();
    const req = await nextRequest(http, '/api/strategies/leaderboard');
    expect(req.request.urlWithParams).toContain('sort=sharpe');
    req.flush(BOARD);
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const links = [...el.querySelectorAll('a.name')].map((a) => a.getAttribute('href'));
    expect(links).toEqual(['/strategies/mom_v2/tearsheet', '/strategies/value_v1/tearsheet']);
    expect(el.textContent).toContain('Ready');
    expect(el.textContent).toContain('Live');
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
