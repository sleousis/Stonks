import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DestroyRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { HaltView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
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

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: HALT_POLL_MS, useValue: 5 },
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
    req.flush([KILL, BREAKER]);
    await tick();
    expect(state.active().length).toBe(2);
    expect(state.kills().map((k) => k.id)).toEqual([1]);

    // The next poll clears it.
    await tick(10);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/halts')) r.flush([]);
    await tick();
    expect(state.kills()).toEqual([]);
  });

  it('keeps the last state when a read fails', async () => {
    (await nextRequest(http, '/api/halts')).flush([KILL]);
    await tick();
    await tick(10);
    for (const r of http.match((x) => x.url.split('?')[0] === '/api/halts')) {
      r.flush({ title: 'x', status: 500 }, { status: 500, statusText: 'err' });
    }
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
