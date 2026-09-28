import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DestroyRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { ToastService } from '../notify/toast.service';
import { TICKET_POLL_MS, TicketCountService } from './ticket-count.service';

const SUMMARY = '/api/tickets/summary';

function fakeDestroyRef() {
  const callbacks: (() => void)[] = [];
  const ref = { onDestroy: (c: () => void) => callbacks.push(c) } as unknown as DestroyRef;
  return { ref, destroy: () => callbacks.forEach((c) => c()) };
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('TicketCountService', () => {
  let http: HttpTestingController;
  let count: TicketCountService;

  function setup(pollMs: number) {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: TICKET_POLL_MS, useValue: pollMs },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    count = TestBed.inject(TicketCountService);
  }

  function flushAll(waiting: number) {
    for (const req of http.match((r) => r.url === SUMMARY)) {
      req.flush({ awaiting_approval: waiting, by_portfolio: {} });
    }
  }

  beforeEach(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
  });

  it('reads the waiting count and polls while the tab is visible', async () => {
    setup(5);
    const { ref, destroy } = fakeDestroyRef();
    count.watch(ref);
    (await nextRequest(http, SUMMARY)).flush({ awaiting_approval: 2, by_portfolio: {} });
    await tick();
    expect(count.waiting()).toBe(2);
    (await nextRequest(http, SUMMARY)).flush({ awaiting_approval: 3, by_portfolio: {} });
    await tick();
    expect(count.waiting()).toBe(3);
    destroy();
    flushAll(3);
  });

  it('pauses while the tab is hidden', async () => {
    setup(5);
    const { ref, destroy } = fakeDestroyRef();
    count.watch(ref);
    (await nextRequest(http, SUMMARY)).flush({ awaiting_approval: 1, by_portfolio: {} });
    setVisibility('hidden');
    flushAll(1);
    await tick(30);
    expect(http.match((r) => r.url === SUMMARY).length).toBe(0);
    destroy();
    setVisibility('visible');
    flushAll(1);
  });

  it('keeps the last count when a read fails, without a toast', async () => {
    setup(0);
    const { ref, destroy } = fakeDestroyRef();
    count.watch(ref);
    (await nextRequest(http, SUMMARY)).flush({ awaiting_approval: 4, by_portfolio: {} });
    await tick();
    const again = count.refresh();
    (await nextRequest(http, SUMMARY)).flush(null, { status: 500, statusText: 'Server Error' });
    await again;
    expect(count.waiting()).toBe(4);
    expect(TestBed.inject(ToastService).toasts()).toEqual([]);
    destroy();
  });

  it('takes a count the approvals page already knows', () => {
    setup(0);
    count.set(6);
    expect(count.waiting()).toBe(6);
    count.set(-1);
    expect(count.waiting()).toBe(0);
  });
});
