import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DestroyRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { ToastService } from '../notify/toast.service';
import { TICKET_POLL_MS, TicketCountService } from './ticket-count.service';

const SUMMARY = '/api/tickets/summary';
const DRAFTS = '/api/orders/drafts';

function fakeDestroyRef() {
  const callbacks: (() => void)[] = [];
  const ref = { onDestroy: (c: () => void) => callbacks.push(c) } as unknown as DestroyRef;
  return { ref, destroy: () => callbacks.forEach((c) => c()) };
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

const drafts = (total: number) => ({ items: [], total, limit: 1, offset: 0 });

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

  /** Answer one read: the ticket summary and the suggested orders count. */
  async function answer(tickets: number, suggested = 0) {
    (await nextRequest(http, SUMMARY)).flush({ awaiting_approval: tickets, by_portfolio: {} });
    (await nextRequest(http, DRAFTS)).flush(drafts(suggested));
    await tick();
  }

  function flushAll(waiting: number) {
    for (const req of http.match((r) => r.url === SUMMARY)) {
      req.flush({ awaiting_approval: waiting, by_portfolio: {} });
    }
    for (const req of http.match((r) => r.url === DRAFTS)) req.flush(drafts(0));
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
    await answer(2);
    expect(count.waiting()).toBe(2);
    await answer(3);
    expect(count.waiting()).toBe(3);
    destroy();
    flushAll(3);
  });

  it('counts suggested orders with the tickets, one inbox (F9)', async () => {
    setup(0);
    const { ref, destroy } = fakeDestroyRef();
    count.watch(ref);
    const req = await nextRequest(http, DRAFTS);
    expect(req.request.urlWithParams).toContain('status=pending');
    (await nextRequest(http, SUMMARY)).flush({ awaiting_approval: 2, by_portfolio: {} });
    req.flush(drafts(3));
    await tick();
    expect(count.waiting()).toBe(5);
    destroy();
  });

  it('pauses while the tab is hidden', async () => {
    setup(5);
    const { ref, destroy } = fakeDestroyRef();
    count.watch(ref);
    await answer(1);
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
    await answer(4, 1);
    const again = count.refresh();
    (await nextRequest(http, SUMMARY)).flush(null, { status: 500, statusText: 'Server Error' });
    (await nextRequest(http, DRAFTS)).flush(null, { status: 500, statusText: 'Server Error' });
    await again;
    expect(count.waiting()).toBe(5);
    expect(TestBed.inject(ToastService).toasts()).toEqual([]);
    destroy();
  });

  it('takes a count the approvals page already knows', () => {
    setup(0);
    count.set(6);
    expect(count.waiting()).toBe(6);
    count.set(2, 3);
    expect(count.waiting()).toBe(5);
    count.set(-1);
    expect(count.waiting()).toBe(0);
  });
});
