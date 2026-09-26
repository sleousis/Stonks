import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { HaltView, ScheduleView } from '../../api/models';
import { ScheduleService } from '../../api/schedule.service';
import type { PortfolioRef } from '../../api/portfolios.service';
import { SessionService } from '../../core/auth/session.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { tick } from '../../../testing/http';
import { SCHEDULE_POLL_MS, SessionStrip, countdown, nextJob } from './session-strip';
import { book } from '../../../testing/portfolio-fixtures';

const NOW = Date.parse('2026-09-26T18:00:00Z');

function schedule(): ScheduleView {
  return {
    backend: 'in_process',
    hosted: true,
    recent: [],
    jobs: [
      {
        name: 'tick',
        action: 'tick',
        trigger: 'close +45m',
        next_run_at: '2026-09-28T20:45:00Z',
        next_as_of: null,
      },
      {
        name: 'ingest_prices',
        action: 'ingest_prices',
        trigger: 'close +30m',
        next_run_at: '2026-09-26T20:05:09Z',
        next_as_of: null,
      },
      { name: 'report', action: 'report', trigger: 'x', next_run_at: null, next_as_of: null },
    ],
  };
}

describe('session strip helpers', () => {
  it('picks the job that fires next', () => {
    expect(nextJob(schedule().jobs, NOW)?.name).toBe('ingest_prices');
    expect(nextJob([], NOW)).toBeNull();
  });

  it('writes a short countdown', () => {
    expect(countdown(0)).toBe('now');
    expect(countdown(12_000)).toBe('12s');
    expect(countdown(4 * 60_000 + 9_000)).toBe('4m 09s');
    expect(countdown(2 * 3_600_000 + 5 * 60_000)).toBe('2h 05m');
    expect(countdown(3 * 86_400_000)).toBe('3d 0h');
  });
});

describe('SessionStrip', () => {
  const active = signal<HaltView[]>([]);
  const options = signal<PortfolioRef[]>([]);
  const portfolios = {
    options,
    hasChoice: () => options().length > 1,
    selectedId: () => null,
    current: () => options()[0] ?? null,
    load: vi.fn().mockResolvedValue(undefined),
    select: vi.fn(),
  };
  let overview: ReturnType<typeof vi.fn>;
  const canRead = signal(true);

  async function render() {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: HaltStateService, useValue: { active } },
        { provide: ScheduleService, useValue: { overview } },
        { provide: SCHEDULE_POLL_MS, useValue: 0 },
        { provide: PortfolioContextService, useValue: portfolios },
        { provide: SessionService, useValue: { canRead } },
      ],
    });
    const fixture = TestBed.createComponent(SessionStrip);
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
    overview = vi.fn().mockResolvedValue(schedule());
    active.set([]);
    options.set([]);
    canRead.set(true);
  });

  it('asks nothing of the API while signed out (BUG-1)', async () => {
    canRead.set(false);
    await render();
    expect(overview).not.toHaveBeenCalled();
  });

  afterEach(() => vi.useRealTimers());

  it('shows the next scheduled run with a countdown, read quietly', async () => {
    const el = await render();
    expect(overview).toHaveBeenCalledWith({ limit: 1 }, true);
    const next = el.querySelector<HTMLAnchorElement>('a.next')!;
    expect(next.getAttribute('href')).toBe('/ops/schedule');
    expect(next.textContent).toContain('Ingest prices');
    expect(next.querySelector('.clock')!.textContent).toBe('2h 05m');
    expect(el.querySelector('.strip')!.getAttribute('data-tone')).toBe('calm');
  });

  it('turns red while a kill switch is on and links to the halts page', async () => {
    active.set([{ id: 1, kind: 'kill', scope: 'global', halt: 'all', active: true } as HaltView]);
    const el = await render();
    const strip = el.querySelector('.strip')!;
    expect(strip.getAttribute('data-tone')).toBe('kill');
    expect(strip.textContent).toContain('Kill switch on.');
    expect(strip.querySelector('a.strip-link')!.getAttribute('href')).toBe('/ops/halts');
  });

  it('shows a breaker in amber and hides itself when there is nothing to say', async () => {
    overview.mockRejectedValue(new Error('no scheduler'));
    active.set([
      { id: 2, kind: 'drawdown', scope: 'global', halt: 'buys', active: true } as HaltView,
    ]);
    const el = await render();
    expect(el.querySelector('.strip')!.getAttribute('data-tone')).toBe('halt');
    expect(el.querySelector('a.next')).toBeNull();

    TestBed.resetTestingModule();
    active.set([]);
    const empty = await render();
    expect(empty.querySelector('.strip')).toBeNull();
  });

  it('holds the portfolio picker when there is more than one portfolio', async () => {
    overview.mockRejectedValue(new Error('no scheduler'));
    options.set([
      book({ id: 'pf_default', name: 'Main', trading: 'paper', is_default: true }),
      book({ id: 'pf_live', name: 'Real money', trading: 'live' }),
    ]);
    const el = await render();
    expect(portfolios.load).toHaveBeenCalled();
    const select = el.querySelector<HTMLSelectElement>('#portfolio-picker')!;
    expect(select).not.toBeNull();
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      'My default portfolio',
      'Main',
      'Real money (live)',
    ]);
    expect(el.querySelector('.stamp')!.textContent).toContain('PAPER');
    select.value = 'pf_live';
    select.dispatchEvent(new Event('change'));
    expect(portfolios.select).toHaveBeenCalledWith('pf_live');
  });
});
