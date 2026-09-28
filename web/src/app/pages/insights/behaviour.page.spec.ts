import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { BehaviourBucketView, BehaviourView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { BehaviourPage, findings } from './behaviour.page';

const bucket = (label: string, trades = 0, pnl = 0, win_rate: number | null = null) =>
  ({ label, trades, pnl, win_rate }) as BehaviourBucketView;

function report(over: Partial<BehaviourView> = {}): BehaviourView {
  return {
    portfolio_id: 'pf_1',
    since: null,
    trades: 3,
    open_positions: 0,
    win_rate: 1 / 3,
    total_pnl: -5,
    avg_win: 100,
    avg_loss: -52.5,
    by_holding: [bucket('under a day', 1, -5, 0), bucket('1 to 5 days', 1, 100, 1)],
    by_weekday: [
      bucket('Mon', 1, 100, 1),
      bucket('Tue'),
      bucket('Wed', 2, -105, 0),
      bucket('Thu'),
      bucket('Fri'),
      bucket('Sat'),
      bucket('Sun'),
    ],
    disposition: { avg_days_winners: 2, avg_days_losers: 10, ratio: 5, present: true },
    overtrading: {
      active_days: 3,
      entries_per_active_day: 1,
      max_entries_in_a_day: 1,
      busy_days: 0,
      busy_day_pnl: 0,
      other_day_pnl: -5,
    },
    revenge: bucket('revenge', 1, -5, 0),
    versus_strategies: [
      bucket('with', 1, 100, 1),
      bucket('against', 2, -105, 0),
      bucket('no_view'),
    ],
    against_strategies_cost: -105,
    sources: { manual: 5, broker: 1 },
    ...over,
  };
}

describe('BehaviourPage', () => {
  let fixture: ComponentFixture<BehaviourPage>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: PortfolioContextService,
          useValue: {
            selectedId: () => 'pf_1',
            current: () => null,
            live: () => false,
            state: () => 'ready',
            noBook: () => false,
            query: () => ({ portfolio_id: 'pf_1' }),
          },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(body: BehaviourView): Promise<HTMLElement> {
    fixture = TestBed.createComponent(BehaviourPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/insights/behaviour')).flush(body);
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('shows the summary, the habits and what going against the strategies cost', async () => {
    const el = await render(report());
    const text = el.textContent ?? '';
    expect(text).toContain('Closed trades');
    expect(text).toContain('33%');
    expect(text).toContain('You hold losers 5 times longer than winners.');
    expect(text).toContain('Against the strategies');
    expect(text).toContain('-$105.00');
    // weekend rows only when you traded then
    expect(text).not.toContain('Sat');
  });

  it('says so when there are no closed trades', async () => {
    const el = await render(report({ trades: 0 }));
    expect(el.textContent).toContain('No closed trades yet');
  });

  it('writes plain findings', () => {
    const notes = findings(report());
    expect(notes).toHaveLength(3);
    expect(notes[1]).toContain('within a day of a losing exit');
    expect(
      findings(report({ disposition: { ...report().disposition, present: false } })),
    ).toHaveLength(2);
  });
});
