import { TestBed } from '@angular/core/testing';

import type { OrderView } from '../../api/models';
import { OrderStatus, orderReason, orderStatusView } from './order-status';

describe('orderStatusView', () => {
  it('maps the order lifecycle to pill tones', () => {
    expect(orderStatusView('pending')).toEqual({
      status: 'pending',
      label: 'Pending',
      tone: 'progress',
      reason: null,
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
    });
  });

  it('drops blank reasons and reasons on orders that went through', () => {
    expect(orderStatusView('rejected', '  ').reason).toBeNull();
    expect(orderStatusView('rejected', null).reason).toBeNull();
    expect(orderStatusView('filled', 'stale note').reason).toBeNull();
  });

  it('is case-insensitive and keeps unknown statuses readable', () => {
    expect(orderStatusView('REJECTED').tone).toBe('negative');
    expect(orderStatusView('pending_cancel')).toMatchObject({
      label: 'pending cancel',
      tone: 'neutral',
    });
    expect(orderStatusView(null).label).toBe('Unknown');
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
  });
});
