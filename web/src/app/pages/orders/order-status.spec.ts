import { TestBed } from '@angular/core/testing';

import type { OrderView } from '../../api/models';
import {
  ORDER_STATUS_OPTIONS,
  OrderStatus,
  orderReason,
  orderStatusView,
  stopWords,
} from './order-status';

describe('orderStatusView', () => {
  it('maps the order lifecycle to pill tones', () => {
    expect(orderStatusView('pending')).toEqual({
      status: 'pending',
      label: 'Pending',
      tone: 'progress',
      reason: null,
      note: null,
    });
    expect(orderStatusView('filled').tone).toBe('positive');
    expect(orderStatusView('partially_filled')).toMatchObject({
      label: 'Partially filled',
      tone: 'warn',
    });
    expect(orderStatusView('cancelled').tone).toBe('neutral');
  });

  it('shows a rejected order with its reason', () => {
    expect(orderStatusView('rejected', 'insufficient buying power')).toEqual({
      status: 'rejected',
      label: 'Rejected',
      tone: 'negative',
      reason: 'insufficient buying power',
      note: null,
    });
  });

  it('drops blank reasons and reasons on orders that went through', () => {
    expect(orderStatusView('rejected', '  ').reason).toBeNull();
    expect(orderStatusView('rejected', null).reason).toBeNull();
    expect(orderStatusView('filled', 'stale note').reason).toBeNull();
  });

  it('is case-insensitive and keeps unknown values readable', () => {
    expect(orderStatusView('REJECTED').tone).toBe('negative');
    expect(orderStatusView('held_at_desk')).toMatchObject({
      label: 'held at desk',
      tone: 'neutral',
      note: null,
    });
    expect(orderStatusView(null).label).toBe('No status');
  });

  it('prefers the fine state and explains the unsettled ones', () => {
    expect(orderStatusView('pending', null, 'unknown')).toMatchObject({
      status: 'unknown',
      label: 'Outcome unknown',
      tone: 'warn',
    });
    expect(orderStatusView('pending', null, 'unknown').note).toContain('Nothing is sent again');
    expect(orderStatusView('pending', null, 'pending_cancel')).toMatchObject({
      label: 'Cancelling',
      tone: 'warn',
    });
    expect(orderStatusView('submitted', null, 'accepted').label).toBe('Working');
    expect(orderStatusView('cancelled', 'day order ended', 'expired')).toMatchObject({
      label: 'Expired',
      reason: 'day order ended',
    });
    // Older orders have no state: the status speaks.
    expect(orderStatusView('filled', null, null).label).toBe('Filled');
  });

  it('offers the coarse statuses as filters', () => {
    expect(ORDER_STATUS_OPTIONS.map((o) => o.value)).toEqual([
      'pending',
      'submitted',
      'filled',
      'partially_filled',
      'rejected',
      'cancelled',
    ]);
  });

  it('reads the ledger reason when the API sends one', () => {
    const base: OrderView = {
      client_id: 'c1',
      broker_order_id: null,
      created_at: '',
      updated_at: '',
      limit_price: null,
      order_type: 'market',
      quantity: 1,
      side: 'buy',
      status: 'rejected',
      strategy_id: null,
      strategy_name: null,
      tick_id: null,
      ticker: 'AAPL.US',
    };
    expect(orderReason(base)).toBeNull();
    expect(orderReason({ ...base, status_reason: null })).toBeNull();
    expect(orderReason({ ...base, status_reason: 'market closed' })).toBe('market closed');
  });
});

describe('OrderStatus', () => {
  it('renders the pill and the rejection reason as visible text', () => {
    const fixture = TestBed.createComponent(OrderStatus);
    fixture.componentRef.setInput('status', 'rejected');
    fixture.componentRef.setInput('reason', 'insufficient buying power');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;

    const pill = el.querySelector('app-status-pill');
    expect(pill?.getAttribute('data-tone')).toBe('negative');
    expect(pill?.textContent).toContain('Rejected');
    expect(el.querySelector('.reason')?.textContent).toContain('insufficient buying power');
  });

  it('shows no reason line for a filled order', () => {
    const fixture = TestBed.createComponent(OrderStatus);
    fixture.componentRef.setInput('status', 'filled');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-status-pill')?.getAttribute('data-tone')).toBe('positive');
    expect(el.querySelector('.reason')).toBeNull();
    expect(el.querySelector('.note')).toBeNull();
  });

  it('shows what an unknown outcome means, and hides it when asked', () => {
    const fixture = TestBed.createComponent(OrderStatus);
    fixture.componentRef.setInput('status', 'pending');
    fixture.componentRef.setInput('state', 'unknown');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('app-status-pill')?.textContent).toContain('Outcome unknown');
    expect(el.querySelector('.note')?.textContent).toContain('did not answer');

    fixture.componentRef.setInput('showNote', false);
    fixture.detectChanges();
    expect(el.querySelector('.note')).toBeNull();
  });
});

describe('stopWords', () => {
  const money = (v: number) => `$${v.toFixed(2)}`;

  it('names a protective stop and when it trades', () => {
    expect(
      stopWords({ side: 'sell', order_type: 'stop', stop_price: 90.5, protective: true }, money),
    ).toEqual({
      label: 'Protective stop',
      trigger: 'Sells if the price falls to $90.50.',
      lasts: 'Works at the broker until the position closes, and follows its size.',
    });
    expect(
      stopWords({ side: 'buy', order_type: 'stop', stop_price: 110, protective: true }, money)
        ?.trigger,
    ).toBe('Buys back if the price rises to $110.00.');
  });

  it('keeps other orders plain', () => {
    expect(
      stopWords({ side: 'buy', order_type: 'market', stop_price: null, protective: false }, money),
    ).toBeNull();
    expect(
      stopWords({ side: 'sell', order_type: 'stop', stop_price: 5, protective: false }, money),
    ).toMatchObject({ label: 'Stop order', lasts: null });
  });
});
