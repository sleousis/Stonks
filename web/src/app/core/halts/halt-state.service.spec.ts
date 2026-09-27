import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DestroyRef, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { HaltView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, page, tick } from '../../../testing/http';
import { SessionService } from '../auth/session.service';
import { HALT_POLL_MS, HaltStateService, haltScopeText } from './halt-state.service';

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

  beforeEach(() => {
    canRead.set(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: HALT_POLL_MS, useValue: 5 },
        { provide: SessionService, useValue: { canRead } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    state = TestBed.inject(HaltStateService);
    const callbacks: (() => void)[] = [];
    destroy = () => callbacks.forEach((c) => c());
    state.watch({ onDestroy: (c: () => void) => callbacks.push(c) } as unknown as DestroyRef);
  });

  afterEach(() => destroy());

  it('keeps the active kill switches and polls again', async () => {
    const req = await nextRequest(http, '/api/halts');
    expect(req.request.urlWithParams).toContain('include_cleared=false');
    req.flush(page([KILL, BREAKER]));
    await tick();
    expect(state.active().length).toBe(2);
    expect(state.kills().map((k) => k.id)).toEqual([1]);

    // The next poll clears it.
    await tick(10);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/halts')) r.flush(page([]));
    await tick();
    expect(state.kills()).toEqual([]);
  });

  it('keeps the last state when a read fails', async () => {
    (await nextRequest(http, '/api/halts')).flush(page([KILL]));
    await tick();
    await tick(10);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/halts')) {
      r.flush({ title: 'x', status: 500 }, { status: 500, statusText: 'err' });
    }
    await tick();
    expect(state.kills().length).toBe(1);
  });

  it('reads nothing while signed out, then reads once signed in (BUG-1)', async () => {
    const halts = (x: { url: string }) => x.url.split('?')[0] === '/api/halts';
    (await nextRequest(http, '/api/halts')).flush(page([]));
    canRead.set(false);
    await tick(5);
    for (const r of http.match(halts)) r.flush(page([]));
    await tick(20);
    expect(http.match(halts)).toEqual([]);
    canRead.set(true);
    TestBed.tick();
    await tick(10);
    const pending = http.match(halts);
    expect(pending.length).toBeGreaterThan(0);
    for (const r of pending) r.flush(page([KILL]));
    await tick();
    expect(state.kills().length).toBe(1);
  });

  it('names the scope', () => {
    expect(haltScopeText(KILL)).toBe('Portfolio pf_default');
    expect(haltScopeText({ scope: 'global', portfolio_id: null, user_id: null })).toBe('Global');
    expect(haltScopeText({ scope: 'user', portfolio_id: null, user_id: 'usr_a' })).toBe(
      'User usr_a',
    );
  });
});
