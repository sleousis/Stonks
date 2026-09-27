import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { FeedItemView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import type { TickRun } from '../../api/models';
import { formatWeekday } from '../../core/format/format';
import { NotificationFeedService } from '../../core/notify/notification-feed.service';
import { TradingDayService } from '../../core/schedule/trading-day.service';
import { SignalsCard, runDetail, todaysRuns, todaysSignals } from './signals-card';

function run(overrides: Partial<TickRun>): TickRun {
  return {
    id: 't1',
    started_at: new Date(Date.now() - 60_000).toISOString(),
    finished_at: new Date().toISOString(),
    status: 'ok',
    summary: { orders_placed: 3, fills: 1 },
    ...overrides,
  };
}

function item(overrides: Partial<FeedItemView>): FeedItemView {
  return {
    id: 1,
    category: 'signal',
    level: 'info',
    title: 'Buy AAPL.US',
    message: 'momentum-v3 entered AAPL.US',
    deep_link: '/strategies/momentum-v3',
    read_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('todaysSignals', () => {
  const now = new Date('2026-09-26T12:00:00Z');

  it('keeps signals from the last 24 hours only', () => {
    const items = [
      item({ id: 1, created_at: '2026-09-26T11:00:00Z' }),
      item({ id: 2, created_at: '2026-09-25T21:00:00Z' }),
      item({ id: 3, created_at: '2026-09-25T11:00:00Z' }),
      item({ id: 4, category: 'order', created_at: '2026-09-26T11:00:00Z' }),
    ];
    expect(todaysSignals(items, now).map((i) => i.id)).toEqual([1, 2]);
  });
});

describe('trading runs on the blotter', () => {
  it('keeps runs from the last 24 hours and says what they did', () => {
    const now = new Date('2026-09-26T12:00:00Z');
    const runs = [
      run({ id: 'a', started_at: '2026-09-26T11:00:00Z' }),
      run({ id: 'b', started_at: '2026-09-24T11:00:00Z' }),
    ];
    expect(todaysRuns(runs, now).map((r) => r.id)).toEqual(['a']);
    expect(runDetail(run({}))).toBe('3 orders, 1 fill.');
    expect(runDetail(run({ status: 'running' }))).toContain('now');
    expect(runDetail(run({ status: 'error', summary: { error: 'Broker down' } }))).toBe(
      'Broker down',
    );
  });
});

describe('SignalsCard', () => {
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
    // The strip reads the schedule. Here it has been read: nothing to wait for.
    TestBed.inject(TradingDayService)['settledSignal'].set(true);
  });

  afterEach(() => controller.verify());

  it('keeps the loading rows until the schedule is read, so nothing jumps', async () => {
    TestBed.inject(TradingDayService)['settledSignal'].set(false);
    const fixture = TestBed.createComponent(SignalsCard);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/notifications')).flush({ items: [], unread_count: 0 });
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [],
      total: 0,
      limit: 10,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('app-loading-state')).not.toBeNull();
    TestBed.inject(TradingDayService)['settledSignal'].set(true);
    fixture.detectChanges();
    expect(el.querySelector('app-loading-state')).toBeNull();
  });

  it('lists today signals, marks new ones, and marks them read', async () => {
    const counter = TestBed.inject(NotificationFeedService);
    counter.set(5);
    const fixture = TestBed.createComponent(SignalsCard);
    fixture.detectChanges();
    const feed = await nextRequest(controller, '/api/notifications');
    expect(feed.request.urlWithParams).toContain('limit=100');
    feed.flush({
      items: [item({ id: 7 }), item({ id: 8, read_at: '2026-09-26T10:00:00Z', title: 'Sell X' })],
      unread_count: 1,
    });
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [],
      total: 0,
      limit: 10,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('li')).toHaveLength(2);
    expect(el.querySelectorAll('li.unread')).toHaveLength(1);
    expect(el.querySelector('a.title')?.getAttribute('href')).toBe('/strategies/momentum-v3');

    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Mark all'))!.click();
    const read = await nextRequest(controller, '/api/notifications/read', 'POST');
    expect(read.request.body).toEqual({ ids: [7] });
    read.flush({ updated: 1, unread_count: 2 });
    (await nextRequest(controller, '/api/notifications')).flush({ items: [], unread_count: 2 });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('No signals today');
    // The bell takes the reply's count (UX-32).
    expect(counter.unread()).toBe(2);
  });

  it('counts down to the next trading run, not the earliest system job (UX-08)', async () => {
    const soon = (min: number) => new Date(Date.now() + min * 60_000).toISOString();
    TestBed.inject(TradingDayService)['jobsSignal'].set([
      {
        action: 'connections_sync',
        name: 'connections_sync',
        next_run_at: soon(5),
        next_as_of: null,
        trigger: 'interval',
      },
      {
        action: 'health',
        name: 'health',
        next_run_at: soon(10),
        next_as_of: null,
        trigger: 'interval',
      },
      { action: 'tick', name: 'tick', next_run_at: soon(90), next_as_of: null, trigger: 'daily' },
    ]);
    const fixture = TestBed.createComponent(SignalsCard);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/notifications')).flush({ items: [], unread_count: 0 });
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [],
      total: 0,
      limit: 10,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    const next = (fixture.nativeElement as HTMLElement).querySelector('.row.next')!;
    expect(next.querySelector('.title')?.textContent?.trim()).toBe('Next: Trading run');
    expect(next.textContent).toContain('1h 2');
    expect(next.textContent).not.toMatch(/sync|Health|Tick/);
  });

  it('names the weekday of a trading run on another day', async () => {
    const at = new Date(Date.now() + 3 * 86_400_000).toISOString();
    TestBed.inject(TradingDayService)['jobsSignal'].set([
      { action: 'tick', name: 'tick', next_run_at: at, next_as_of: null, trigger: 'daily' },
    ]);
    const fixture = TestBed.createComponent(SignalsCard);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/notifications')).flush({ items: [], unread_count: 0 });
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [],
      total: 0,
      limit: 10,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    const time = (fixture.nativeElement as HTMLElement).querySelector('.row.next .time')!;
    expect(time.querySelector('.weekday')?.textContent?.trim()).toBe(formatWeekday(at));
  });

  it('puts runs and signals in one time line, newest first', async () => {
    const fixture = TestBed.createComponent(SignalsCard);
    fixture.detectChanges();
    (await nextRequest(controller, '/api/notifications')).flush({
      items: [item({ id: 1, created_at: new Date(Date.now() - 3_600_000).toISOString() })],
      unread_count: 1,
    });
    (await nextRequest(controller, '/api/ticks')).flush({
      items: [run({ id: 'tk9' })],
      total: 1,
      limit: 10,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const rows = [...el.querySelectorAll('li.row')];
    expect(rows.map((r) => r.getAttribute('data-kind'))).toEqual(['run', 'signal']);
    expect(rows[0].querySelector('a.title')?.getAttribute('href')).toBe('/orders/ticks/tk9');
    expect(rows[0].querySelector('app-status-pill')?.textContent).toContain('succeeded');
    expect(rows[0].textContent).toContain('3 orders, 1 fill.');
  });
});
