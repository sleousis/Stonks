import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DestroyRef, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { HaltView, MeView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { HaltsService } from '../../api/halts.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { TRADER } from '../../../testing/auth-fixtures';
import { book } from '../../../testing/portfolio-fixtures';
import { SessionService } from '../auth/session.service';
import { PortfolioContextService } from '../portfolio/portfolio-context.service';
import { HALT_POLL_MS, HaltStateService } from './halt-state.service';

const KILL = {
  id: 1,
  kind: 'kill',
  scope: 'portfolio',
  portfolio_id: 'pf_default',
  user_id: null,
  halt: 'all',
  active: true,
} as HaltView;
const BREAKER = { ...KILL, id: 2, kind: 'drawdown', halt: 'buys' } as HaltView;

describe('HaltStateService', () => {
  let http: HttpTestingController;
  let state: HaltStateService;
  let destroy: () => void;
  const canRead = signal(true);
  const me = signal<MeView | null>(TRADER);
  const options = signal([book({ id: 'pf_default', name: 'Main book', is_default: true })]);

  const halts = (x: { url: string }) => x.url.split('?')[0] === '/api/halts';
  /** Fire the poll interval once; only setInterval is faked, so requests still flow. */
  const poll = () => vi.advanceTimersByTime(5);

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    canRead.set(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: HALT_POLL_MS, useValue: 5 },
        { provide: SessionService, useValue: { canRead, me } },
        { provide: PortfolioContextService, useValue: { options } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    state = TestBed.inject(HaltStateService);
    const callbacks: (() => void)[] = [];
    destroy = () => callbacks.forEach((c) => c());
    state.watch({ onDestroy: (c: () => void) => callbacks.push(c) } as unknown as DestroyRef);
  });

  afterEach(() => {
    destroy();
    vi.useRealTimers();
  });

  it('keeps the active kill switches and polls again', async () => {
    const req = await nextRequest(http, '/api/halts');
    expect(req.request.urlWithParams).toContain('include_cleared=false');
    req.flush(page([KILL, BREAKER]));
    await tick();
    expect(state.active().length).toBe(2);
    expect(state.kills().map((k) => k.id)).toEqual([1]);
    expect(state.killOn()).toBe(true);

    // The next poll clears it.
    poll();
    (await nextRequest(http, '/api/halts')).flush(page([]));
    await tick();
    expect(state.kills()).toEqual([]);
    expect(state.killOn()).toBe(false);
  });

  it('keeps the last state when a read fails', async () => {
    (await nextRequest(http, '/api/halts')).flush(page([KILL]));
    await tick();
    poll();
    (await nextRequest(http, '/api/halts')).flush(
      { title: 'x', status: 500 },
      { status: 500, statusText: 'err' },
    );
    await tick();
    expect(state.kills().length).toBe(1);
  });

  it('skips a poll while a read is still in flight', async () => {
    const first = await nextRequest(http, '/api/halts');
    poll();
    poll();
    await tick(5);
    expect(http.match(halts)).toEqual([]);
    first.flush(page([]));
    await tick();
    poll();
    (await nextRequest(http, '/api/halts')).flush(page([KILL]));
    await tick();
    expect(state.kills().length).toBe(1);
  });

  it('reads nothing while signed out, then reads once signed in (BUG-1)', async () => {
    (await nextRequest(http, '/api/halts')).flush(page([]));
    canRead.set(false);
    TestBed.tick();
    poll();
    poll();
    await tick(5);
    expect(http.match(halts)).toEqual([]);
    canRead.set(true);
    TestBed.tick();
    (await nextRequest(http, '/api/halts')).flush(page([KILL]));
    await tick();
    expect(state.kills().length).toBe(1);
  });

  it('names the portfolio in the summary, never its id (UX-17)', async () => {
    (await nextRequest(http, '/api/halts')).flush(page([KILL]));
    await tick();
    expect(state.summary()!.text).toContain('Portfolio Main book');
    expect(state.summary()!.text).not.toContain('pf_');
  });
});

describe('HaltStateService sequence guard (UX-52)', () => {
  it('out-of-order refreshes keep the newest', async () => {
    const answers: ((v: HaltView[]) => void)[] = [];
    TestBed.configureTestingModule({
      providers: [
        { provide: HALT_POLL_MS, useValue: 0 },
        { provide: SessionService, useValue: { canRead: () => true, me: () => TRADER } },
        { provide: PortfolioContextService, useValue: { options: () => [] } },
        {
          provide: HaltsService,
          useValue: {
            list: () => new Promise<HaltView[]>((resolve) => answers.push(resolve)),
          },
        },
      ],
    });
    const state = TestBed.inject(HaltStateService);
    const older = state.refresh(); // started while the kill switch was on
    const newer = state.refresh(); // started after it was resumed
    answers[1]([]);
    await newer;
    answers[0]([KILL]);
    await older;
    expect(state.active()).toEqual([]);
    expect(state.killOn()).toBe(false);
  });
});
