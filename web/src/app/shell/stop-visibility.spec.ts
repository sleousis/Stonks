import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import type { MeView } from '../api/models';
import type { PortfolioRef } from '../api/portfolios.service';
import type { SubscriptionView } from '../api/subscriptions.service';
import { SubscriptionsService } from '../api/subscriptions.service';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { PortfolioContextService } from '../core/portfolio/portfolio-context.service';
import { ADMIN, TRADER } from '../../testing/auth-fixtures';
import { sub } from '../../testing/home-fixtures';
import { tick } from '../../testing/http';
import { book } from '../../testing/portfolio-fixtures';
import { StopTradingVisibility } from './stop-visibility';

describe('StopTradingVisibility (FR-06, NB-15)', () => {
  const me = signal<MeView | null>(TRADER);
  const options = signal<PortfolioRef[]>([]);
  let list: ReturnType<typeof vi.fn>;

  function create() {
    TestBed.configureTestingModule({
      providers: [
        {
          provide: SessionService,
          useValue: {
            me,
            can: (p: Permission) => allowed(me(), p),
            isAdmin: () => me()?.role === 'admin',
          },
        },
        { provide: PortfolioContextService, useValue: { options } },
        { provide: SubscriptionsService, useValue: { list } },
      ],
    });
    return TestBed.inject(StopTradingVisibility);
  }

  async function settle() {
    TestBed.tick();
    await tick();
    TestBed.tick();
  }

  beforeEach(() => {
    localStorage.removeItem(`stonks.stopShown.${TRADER.user_id}`);
    me.set(TRADER);
    options.set([]);
    list = vi.fn().mockResolvedValue([] as SubscriptionView[]);
  });

  it('hides the control from a new trader with nothing that trades', async () => {
    const v = create();
    await settle();
    expect(v.show()).toBe(false);
    // No portfolio: no follow can trade, so nothing is asked.
    expect(list).not.toHaveBeenCalled();
  });

  it('keeps it hidden while every follow is Alerts only', async () => {
    options.set([book({ id: 'pf_1', name: 'Paper' })]);
    list.mockResolvedValue([sub({ mode: 'notify' })]);
    const v = create();
    await settle();
    expect(list).toHaveBeenCalledTimes(1);
    expect(v.show()).toBe(false);
  });

  it('shows it once a follow trades, and keeps it shown', async () => {
    options.set([book({ id: 'pf_1', name: 'Paper' })]);
    list.mockResolvedValue([sub({ mode: 'paper' })]);
    const v = create();
    await settle();
    expect(v.show()).toBe(true);
    expect(localStorage.getItem(`stonks.stopShown.${TRADER.user_id}`)).toBe('1');

    // Next visit: shown straight away, even before anything is read.
    TestBed.resetTestingModule();
    options.set([]);
    const again = create();
    await settle();
    expect(again.show()).toBe(true);
  });

  it('shows it for a broker portfolio and always for admins', async () => {
    options.set([book({ id: 'pf_b', name: 'IBKR', kind: 'broker' })]);
    expect(create().show()).toBe(true);
    TestBed.resetTestingModule();
    options.set([]);
    me.set(ADMIN);
    expect(create().show()).toBe(true);
  });

  it('never shows it to a viewer', async () => {
    me.set({ ...TRADER, role: 'viewer', scopes: ['read'] });
    options.set([book({ id: 'pf_b', name: 'IBKR', kind: 'broker' })]);
    expect(create().show()).toBe(false);
  });
});
