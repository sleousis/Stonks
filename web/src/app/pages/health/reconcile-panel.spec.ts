import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { ReconcileReportView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { ReconcilePanel, itemWords, kindWords, reconcileState } from './reconcile-panel';

function report(over: Partial<ReconcileReportView> = {}): ReconcileReportView {
  return {
    id: 'rec_1',
    portfolio_id: 'pf_live',
    kind: 'sod',
    as_of: '2026-09-28',
    taken_at: '2026-09-28T12:30:00+00:00',
    status: 'clean',
    items: [],
    explained: [],
    external: { positions: {}, orders: [] },
    summary: {},
    detail: null,
    halt_id: null,
    paused: [],
    ...over,
  };
}

const DRIFT = report({
  id: 'rec_2',
  kind: 'eod',
  status: 'drift',
  halt_id: 7,
  paused: ['sub_1'],
  items: [
    {
      kind: 'position_qty',
      key: 'AAPL.US',
      ours: 10,
      broker: 9,
      material: true,
      explained: false,
      detail: 'Stonks owns 10, the broker holds 9',
    },
  ],
  external: {
    positions: { 'MSFT.US': 3 },
    orders: [{ broker_order_id: '77', ticker: 'MSFT.US', side: 'buy', quantity: 5 }],
  },
});

describe('reconcile words', () => {
  it('names each status, kind and item', () => {
    expect(reconcileState(report()).label).toBe('Matches');
    expect(reconcileState(DRIFT)).toEqual({
      status: 'halted',
      label: 'Drift, buys halted',
      tone: 'negative',
    });
    expect(reconcileState(report({ status: 'outage' })).label).toBe('Broker unreachable');
    expect(reconcileState(report({ status: 'fault' })).label).toBe('Broker fault');
    expect(reconcileState(report({ status: 'warn' })).label).toBe('Look at it');
    expect(kindWords('eod')).toBe('End of day');
    expect(itemWords(DRIFT.items[0])).toBe('Position differs');
    expect(itemWords({ ...DRIFT.items[0], kind: 'odd_thing' })).toBe('odd thing');
  });
});

describe('ReconcilePanel', () => {
  let fixture: ComponentFixture<ReconcilePanel>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(reports: ReconcileReportView[]): Promise<HTMLElement> {
    fixture = TestBed.createComponent(ReconcilePanel);
    fixture.detectChanges();
    (await nextRequest(http, '/api/reconcile/reports')).flush(reports);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('stays hidden while there is no report', async () => {
    const el = await render([]);
    expect(el.querySelector('section')).toBeNull();
  });

  it('shows drift with its items, the halt and the owner holdings kept apart', async () => {
    const el = await render([DRIFT, report()]);
    const cards = [...el.querySelectorAll('.report')];
    expect(cards.length).toBe(2);
    expect(el.querySelector('.drift')?.textContent).toContain('1 with drift');

    const drift = cards[0];
    expect(drift.getAttribute('data-state')).toBe('halted');
    expect(drift.textContent).toContain('End of day');
    expect(drift.textContent).toContain('Position differs');
    expect(drift.textContent).toContain('AAPL.US');
    expect(drift.textContent).toContain('Halt #7');
    expect(drift.textContent).toContain('2 of your own holdings or orders');

    const clean = cards[1];
    expect(clean.textContent).toContain('Matches');
    expect(clean.querySelector('.items')).toBeNull();
  });
});
