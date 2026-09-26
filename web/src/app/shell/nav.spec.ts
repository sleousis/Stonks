import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { SessionService } from '../core/auth/session.service';
import { ADMIN, TRADER } from '../../testing/auth-fixtures';
import { Nav } from './nav';

describe('Nav', () => {
  const me = signal<MeView | null>(null);
  const session = { me, isAdmin: () => me()?.role === 'admin' };

  beforeEach(() => {
    localStorage.removeItem('stonks.navAdvanced');
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    });
  });

  function render(user: MeView | null) {
    me.set(user);
    const fixture = TestBed.createComponent(Nav);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function mine(el: HTMLElement) {
    return [...el.querySelectorAll('ul[aria-label="Your pages"] a span')].map((s) => s.textContent);
  }

  it('puts Home and Profile first, and Users for admins only', () => {
    expect(mine(render(TRADER))).toEqual(['Home', 'Profile']);
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    });
    expect(mine(render(ADMIN))).toEqual(['Home', 'Profile', 'Users']);
  });

  it('folds advanced pages away for traders signed in with a session', () => {
    const el = render(TRADER);
    expect(el.querySelector('details')!.open).toBe(false);
    expect(el.querySelector('summary')!.textContent).toContain('Advanced');
  });

  it('keeps them open for admins, tokens and dev mode', () => {
    expect(render(ADMIN).querySelector('details')!.open).toBe(true);
    me.set({ ...TRADER, via: 'token' });
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    });
    expect(render({ ...TRADER, via: 'token' }).querySelector('details')!.open).toBe(true);
  });

  it('remembers the trader choice', () => {
    localStorage.setItem('stonks.navAdvanced', '1');
    expect(render(TRADER).querySelector('details')!.open).toBe(true);
  });
});
