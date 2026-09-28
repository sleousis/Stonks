import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { HaltView, MeView, ScheduleView, ScheduledJobView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ScheduleService } from '../../api/schedule.service';
import type { PortfolioRef } from '../../api/portfolios.service';
import { type Permission, allowed } from '../../core/auth/permissions';
import { SessionService } from '../../core/auth/session.service';
import { HaltStateService } from '../../core/halts/halt-state.service';
import { type HaltScope, haltScopeText, haltSummary } from '../../core/halts/halt-view';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, tick } from '../../../testing/http';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { activeFormat, browserFormat } from '../../core/format/format';
import { DEFAULT_KILL_REASON } from './kill-sheet';
import { SCHEDULE_POLL_MS, SessionStrip, countdown, nextJob, sessionPhase } from './session-strip';
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

const MARKET = {
  calendar: 'XNYS',
  is_open: false,
  today: {
    date: '2026-09-28',
    pre_open: '2026-09-28T08:00:00Z',
    open: '2026-09-28T13:30:00Z',
    close: '2026-09-28T20:00:00Z',
  },
  next: {
    date: '2026-09-29',
    pre_open: '2026-09-29T08:00:00Z',
    open: '2026-09-29T13:30:00Z',
    close: '2026-09-29T20:00:00Z',
  },
};

describe('sessionPhase', () => {
  beforeEach(() => activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' }));
  afterEach(() => activeFormat.set(browserFormat()));

  it('before pre-open: closed, opens today', () => {
    const p = sessionPhase(MARKET, Date.parse('2026-09-28T06:00:00Z'));
    expect(p.phase).toBe('closed');
    expect(p.event).toBe('Opens');
    expect(p.at).toBe('13:30');
    expect(p.track?.now).toBe(0);
  });

  it('pre-open, then open with the time to the close', () => {
    const pre = sessionPhase(MARKET, Date.parse('2026-09-28T09:00:00Z'));
    expect(pre.phase).toBe('pre');
    expect(pre.label).toBe('Pre-open');
    const open = sessionPhase(MARKET, Date.parse('2026-09-28T19:00:00Z'));
    expect(open.phase).toBe('open');
    expect(open.label).toBe('Market open');
    expect(open.event).toBe('Closes');
    expect(open.at).toBe('20:00');
    expect(open.inMs).toBe(3_600_000);
    expect(open.track?.openAt).toBeCloseTo(5.5 / 12);
  });

  it('after the close or on a day off: the next open with its weekday', () => {
    const after = sessionPhase(MARKET, Date.parse('2026-09-28T21:00:00Z'));
    expect(after.phase).toBe('closed');
    expect(after.at).toBe('Tue 13:30');
    const off = sessionPhase({ ...MARKET, today: null }, Date.parse('2026-09-27T12:00:00Z'));
    expect(off.track).toBeNull();
    expect(off.at).toBe('Tue 13:30');
  });
});

describe('SessionStrip', () => {
  const active = signal<HaltView[]>([]);
  const options = signal<PortfolioRef[]>([]);
  const me = signal<MeView | null>(TRADER);
  const current = computed(
    () => options().find((p) => p.is_default) ?? (options().length === 1 ? options()[0] : null),
  );
  const portfolios = {
    options,
    hasChoice: () => options().length > 1,
    selectedId: () => null,
    current,
    live: computed(() => current()?.trading === 'live'),
    load: vi.fn().mockResolvedValue(undefined),
    select: vi.fn(),
  };
  const names = computed(() => new Map(options().map((p) => [p.id, p.name] as const)));
  const halts = {
    active,
    killOn: computed(() => active().some((h) => h.kind === 'kill' && h.active)),
    scopeText: computed(() => (h: HaltScope) => haltScopeText(h, names(), me()?.user_id)),
    summary: computed(() => haltSummary(active(), (h) => haltScopeText(h, names(), me()?.user_id))),
    add: vi.fn((h: HaltView) => active.update((list) => [...list, h])),
    refresh: vi.fn().mockResolvedValue(undefined),
  };
  let overview: ReturnType<typeof vi.fn>;
  const canRead = signal(true);
  const session = {
    canRead,
    me,
    can: (p: Permission) => allowed(me(), p),
    isAdmin: () => me()?.role === 'admin',
    csrfToken: () => null,
  };

  async function render() {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: HaltStateService, useValue: halts },
        { provide: ScheduleService, useValue: { overview } },
        { provide: SCHEDULE_POLL_MS, useValue: 0 },
        { provide: PortfolioContextService, useValue: portfolios },
        { provide: SessionService, useValue: session },
      ],
    });
    const fixture = TestBed.createComponent(SessionStrip);
    fixture.detectChanges();
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  async function renderEl() {
    return (await render()).nativeElement as HTMLElement;
  }

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
    activeFormat.set({ locale: 'en-US', timeZone: 'UTC', dateStyle: 'iso' });
    overview = vi.fn().mockResolvedValue(schedule());
    active.set([]);
    options.set([]);
    me.set(TRADER);
    canRead.set(true);
    halts.add.mockClear();
    halts.refresh.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
    activeFormat.set(browserFormat());
  });

  it('asks nothing of the API while signed out (BUG-1)', async () => {
    canRead.set(false);
    await render();
    expect(overview).not.toHaveBeenCalled();
  });

  it('counts down to the next trading run, read quietly (UX-08)', async () => {
    const el = await renderEl();
    expect(overview).toHaveBeenCalledWith({ limit: 1 }, true);
    const next = el.querySelector<HTMLAnchorElement>('a.next')!;
    expect(next.getAttribute('href')).toBe('/ops/schedule');
    expect(next.textContent).toContain('Trading run');
    expect(next.textContent).not.toContain('Price update');
    expect(next.textContent).toContain('Mon 20:45');
    expect(next.querySelector('.clock')!.textContent).toBe('2d 2h');
    expect(next.getAttribute('aria-label')).toBe(
      'Next trading run at Mon 20:45. Open the schedule.',
    );
    expect(el.querySelector('.strip')!.getAttribute('data-tone')).toBe('calm');
  });

  it("with connections_sync at 14:00 and tick at 16:45, the strip shows 'Trading run 16:45'", async () => {
    vi.setSystemTime(Date.parse('2026-09-28T12:00:00Z'));
    overview.mockResolvedValue({
      ...schedule(),
      jobs: [job('connections_sync', '2026-09-28T14:00:00Z'), job('tick', '2026-09-28T16:45:00Z')],
    });
    const el = await renderEl();
    const next = el.querySelector('a.next')!;
    expect(next.querySelector('.job')!.textContent).toBe('Trading run');
    expect(next.querySelector('.at')!.textContent).toBe('16:45');
    expect(el.textContent).not.toContain('Broker sync');
    expect(el.textContent).not.toMatch(/\btick\b/i);
  });

  it('gives admins the next system job in a quieter slot, only when it comes first', async () => {
    me.set(ADMIN);
    vi.setSystemTime(Date.parse('2026-09-28T12:00:00Z'));
    overview.mockResolvedValue({
      ...schedule(),
      jobs: [
        job('connections_sync', '2026-09-28T14:00:00Z'),
        job('tick', '2026-09-28T16:45:00Z'),
        job('backup', '2026-09-28T23:00:00Z'),
      ],
    });
    const el = await renderEl();
    expect(el.querySelector('a.other')!.textContent).toContain('Then Broker sync 14:00');
    expect(el.querySelector('a.next .job')!.textContent).toBe('Trading run');
  });

  it('holds the phase and countdown places until the schedule is read', async () => {
    let answer!: (v: unknown) => void;
    overview.mockReturnValue(new Promise((r) => (answer = r)));
    const fixture = await render();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('.hold').length).toBe(2);
    expect(el.querySelector('.hold')!.getAttribute('aria-hidden')).toBe('true');
    answer(schedule());
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.hold')).toBeNull();
    expect(el.querySelector('a.next .job')!.textContent).toBe('Trading run');
  });

  it('says so plainly when no trading run is scheduled', async () => {
    overview.mockResolvedValue({ ...schedule(), jobs: [job('health', '2026-09-26T19:00:00Z')] });
    const el = await renderEl();
    expect(el.querySelector('a.next')!.textContent).toContain('No trading run scheduled');
    expect(el.querySelector('.clock')).toBeNull();
  });

  it('shows the market phase and the day track when the schedule has sessions', async () => {
    overview.mockResolvedValue({ ...schedule(), market: { ...MARKET, today: null } });
    const el = await renderEl();
    expect(el.querySelector('.phase')!.textContent).toContain('Market closed');
    expect(el.querySelector('.phase')!.getAttribute('data-phase')).toBe('closed');
    expect(el.querySelector('.track')).toBeNull();
  });

  it('turns red while a kill switch is on and links to the halts page', async () => {
    active.set([{ id: 1, kind: 'kill', scope: 'global', halt: 'all', active: true } as HaltView]);
    const el = await renderEl();
    const strip = el.querySelector('.strip')!;
    expect(strip.getAttribute('data-tone')).toBe('kill');
    expect(strip.textContent).toContain('Trading stopped.');
    expect(strip.textContent).toContain('Every portfolio');
    expect(strip.querySelector('a.strip-link')!.getAttribute('href')).toBe('/ops/halts');
  });

  it('names the portfolio in the banner, never its id (UX-17)', async () => {
    options.set([book({ id: 'pf_default', name: 'Main', is_default: true })]);
    active.set([
      {
        id: 1,
        kind: 'kill',
        scope: 'portfolio',
        portfolio_id: 'pf_default',
        user_id: null,
        halt: 'buys',
        active: true,
      } as HaltView,
    ]);
    const el = await renderEl();
    const text = el.querySelector('.halt')!.textContent!;
    expect(text).toContain('Portfolio Main');
    expect(text).not.toMatch(/pf_|usr_/);
  });

  it('shows a breaker in amber and hides itself when there is nothing to say', async () => {
    overview.mockRejectedValue(new Error('no scheduler'));
    active.set([
      { id: 2, kind: 'drawdown', scope: 'global', halt: 'buys', active: true } as HaltView,
    ]);
    me.set({ ...TRADER, role: 'viewer', scopes: ['read'] });
    const el = await renderEl();
    expect(el.querySelector('.strip')!.getAttribute('data-tone')).toBe('halt');
    expect(el.querySelector('a.next')).toBeNull();

    TestBed.resetTestingModule();
    active.set([]);
    const empty = await renderEl();
    expect(empty.querySelector('.strip')).toBeNull();
  });

  it('holds the portfolio picker when there is more than one portfolio', async () => {
    overview.mockRejectedValue(new Error('no scheduler'));
    options.set([
      book({ id: 'pf_default', name: 'Main', trading: 'paper', is_default: true }),
      book({ id: 'pf_live', name: 'Real money', trading: 'live' }),
    ]);
    const el = await renderEl();
    expect(portfolios.load).toHaveBeenCalled();
    const select = el.querySelector<HTMLSelectElement>('#portfolio-picker')!;
    expect(select).not.toBeNull();
    expect([...select.options].map((o) => o.textContent?.trim())).toEqual([
      'Main',
      'Real money (live)',
    ]);
    expect(el.querySelector('.pick .stamp')!.textContent).toContain('PAPER');
    select.value = 'pf_live';
    select.dispatchEvent(new Event('change'));
    expect(portfolios.select).toHaveBeenCalledWith('pf_live');
  });

  describe('Stop trading (UX-01)', () => {
    it('Stop trading opens the kill sheet in one tap at 375px, and the POST carries the picked portfolio', async () => {
      options.set([
        book({ id: 'pf_other', name: 'Side book' }),
        book({ id: 'pf_main', name: 'Main book', is_default: true }),
      ]);
      const fixture = await render();
      const el = fixture.nativeElement as HTMLElement;
      // Not behind a menu or a fold: a labelled button right in the strip.
      const stop = el.querySelector<HTMLButtonElement>('.strip button.stop')!;
      expect(stop.textContent!.trim()).toBe('Stop trading');
      expect(el.querySelector('#kill-sheet-title')).toBeNull();

      stop.click();
      fixture.detectChanges();
      expect(el.querySelector('#kill-sheet-title')!.textContent).toBe('Stop trading');
      const picked = el.querySelector<HTMLInputElement>('input[name="kill-sheet-scope"]:checked')!;
      expect(picked.value).toBe('portfolio');
      expect(picked.closest('label')!.textContent).toContain('Main book');
      // The ticket: scope, what stops, reason and the stamp.
      const ticket = el.querySelector('[aria-label="Stop trading ticket"]')!;
      expect(ticket.textContent).toContain('Portfolio Main book');
      expect(ticket.textContent).toContain('All new orders');
      expect(ticket.textContent).toContain(DEFAULT_KILL_REASON);
      expect(ticket.querySelector('.stamp')!.textContent).toContain('PAPER');

      el.querySelector<HTMLFormElement>('.kill-form')!.dispatchEvent(new Event('submit'));
      const http = TestBed.inject(HttpTestingController);
      const req = await nextRequest(http, '/api/halts/kill', 'POST');
      expect(req.request.body).toEqual({
        scope: 'portfolio',
        portfolio_id: 'pf_main',
        buys_only: false,
        reason: DEFAULT_KILL_REASON,
      });
      const halt = {
        id: 9,
        kind: 'kill',
        scope: 'portfolio',
        portfolio_id: 'pf_main',
        user_id: null,
        halt: 'all',
        active: true,
      } as HaltView;
      req.flush(halt);
      await tick();
      fixture.detectChanges();
      // Red at once, and the control turns into Resume.
      expect(halts.add).toHaveBeenCalledWith(halt);
      expect(el.querySelector('.strip')!.getAttribute('data-tone')).toBe('kill');
      expect(el.querySelector('#kill-sheet-title')).toBeNull();
      const resume = el.querySelector<HTMLAnchorElement>('a.stop.resume')!;
      expect(resume.textContent!.trim()).toBe('Resume');
      expect(resume.getAttribute('href')).toBe('/ops/halts');
    });

    it('stops new buys only across all your portfolios, with an edited reason', async () => {
      options.set([book({ id: 'pf_main', name: 'Main book', is_default: true })]);
      const fixture = await render();
      const el = fixture.nativeElement as HTMLElement;
      el.querySelector<HTMLButtonElement>('button.stop')!.click();
      fixture.detectChanges();
      const pick = (sel: string) => {
        const input = el.querySelector<HTMLInputElement>(sel)!;
        input.checked = true;
        input.dispatchEvent(new Event('change'));
        fixture.detectChanges();
      };
      pick('input[name="kill-sheet-scope"][value="user"]');
      pick('input[name="kill-sheet-stops"][value="buys"]');
      expect(
        el.querySelector('input[name="kill-sheet-stops"][value="buys"]')!.closest('label')!
          .textContent,
      ).toContain('Stop new buys only');
      const reason = el.querySelector<HTMLInputElement>('#kill-sheet-reason')!;
      reason.value = '';
      reason.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      el.querySelector<HTMLFormElement>('.kill-form')!.dispatchEvent(new Event('submit'));
      await tick();
      fixture.detectChanges();
      const http = TestBed.inject(HttpTestingController);
      expect(http.match('/api/halts/kill')).toEqual([]);
      expect(el.querySelector('#kill-sheet-reason-hint')!.textContent).toContain('Say why');

      reason.value = 'Fed day';
      reason.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      const ticket = el.querySelector('[aria-label="Stop trading ticket"]')!;
      expect(ticket.textContent).toContain('Your portfolios');
      expect(ticket.textContent).toContain('New buys');
      expect(ticket.textContent).toContain('Sells and exits');
      el.querySelector<HTMLFormElement>('.kill-form')!.dispatchEvent(new Event('submit'));
      const req = await nextRequest(http, '/api/halts/kill', 'POST');
      expect(req.request.body).toEqual({
        scope: 'user',
        portfolio_id: null,
        buys_only: true,
        reason: 'Fed day',
      });
      req.flush({ id: 3, kind: 'kill', scope: 'user', halt: 'buys', active: true });
      await tick();
    });

    it('wears the brass ring and a LIVE stamp when the shown portfolio is live', async () => {
      options.set([book({ id: 'pf_live', name: 'Real', trading: 'live', is_default: true })]);
      const fixture = await render();
      const el = fixture.nativeElement as HTMLElement;
      const stop = el.querySelector<HTMLButtonElement>('button.stop')!;
      expect(stop.classList).toContain('live');
      stop.click();
      fixture.detectChanges();
      expect(el.querySelector('[aria-label="Stop trading ticket"] .stamp')!.textContent).toContain(
        'LIVE',
      );
    });

    it('lets admins stop every portfolio; viewers never see the control', async () => {
      me.set(ADMIN);
      const fixture = await render();
      const el = fixture.nativeElement as HTMLElement;
      el.querySelector<HTMLButtonElement>('button.stop')!.click();
      fixture.detectChanges();
      expect(el.querySelector('input[name="kill-sheet-scope"][value="global"]')).not.toBeNull();
      // No portfolio listed: the scope starts on all of the user's portfolios.
      expect(
        el.querySelector<HTMLInputElement>('input[name="kill-sheet-scope"]:checked')!.value,
      ).toBe('user');

      TestBed.resetTestingModule();
      me.set({ ...TRADER, role: 'viewer', scopes: ['read'] });
      const viewer = await renderEl();
      expect(viewer.querySelector('.stop')).toBeNull();
      expect(viewer.querySelector('app-kill-sheet')).toBeNull();
    });
  });
});

function job(action: string, at: string | null): ScheduledJobView {
  return { name: action, action, trigger: 'x', next_run_at: at, next_as_of: null };
}
