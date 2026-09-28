import { TestBed } from '@angular/core/testing';

import { MONEY_MASK, formatMoney, formatPercent, moneyHidden } from '../format/format';
import { PRIVACY_STORAGE_KEY, PrivacyService } from './privacy.service';

describe('PrivacyService', () => {
  beforeEach(() => {
    localStorage.clear();
    moneyHidden.set(false);
    delete document.documentElement.dataset['privacy'];
  });

  afterEach(() => {
    localStorage.clear();
    moneyHidden.set(false);
    delete document.documentElement.dataset['privacy'];
  });

  it('hides money amounts but keeps percentages', () => {
    const svc = TestBed.inject(PrivacyService);
    expect(svc.hidden()).toBe(false);
    expect(formatMoney(1234.5)).not.toBe(MONEY_MASK);
    svc.toggle();
    expect(svc.hidden()).toBe(true);
    expect(formatMoney(1234.5)).toBe(MONEY_MASK);
    expect(formatMoney(-3, { signed: true })).toBe(MONEY_MASK);
    expect(formatMoney(null)).toBe('–');
    expect(formatPercent(0.0123)).toContain('1.23');
    expect(document.documentElement.dataset['privacy']).toBe('on');
  });

  it('remembers the choice on this device only', () => {
    TestBed.inject(PrivacyService).set(true);
    expect(localStorage.getItem(PRIVACY_STORAGE_KEY)).toBe('on');
    TestBed.inject(PrivacyService).set(false);
    expect(localStorage.getItem(PRIVACY_STORAGE_KEY)).toBeNull();
    expect(document.documentElement.dataset['privacy']).toBeUndefined();
  });

  it('starts hidden when the device asked for it before', () => {
    localStorage.setItem(PRIVACY_STORAGE_KEY, 'on');
    const svc = TestBed.inject(PrivacyService);
    expect(svc.hidden()).toBe(true);
    expect(moneyHidden()).toBe(true);
  });
});
