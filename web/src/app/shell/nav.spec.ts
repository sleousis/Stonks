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
    expect(mine(render(TRADER))).toEqual([
      'Home',
      'Notifications',
      'Broker connections',
      'Profile',
    ]);
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    });
    expect(mine(render(ADMIN))).toEqual([
      'Home',
      'Notifications',
      'Broker connections',
      'Profile',
      'Users',
    ]);
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

  it('keeps ids unique when the sidebar and the drawer both render (UI-20)', () => {
    me.set(ADMIN);
    const host = document.createElement('div');
    for (const prefix of ['sidebar', 'drawer']) {
      const fixture = TestBed.createComponent(Nav);
      fixture.componentRef.setInput('idPrefix', prefix);
      fixture.detectChanges();
      host.append(fixture.nativeElement as HTMLElement);
    }
    const ids = [...host.querySelectorAll('[id]')].map((e) => e.id);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
    for (const list of host.querySelectorAll('ul[aria-labelledby]')) {
      expect(host.querySelector('#' + list.getAttribute('aria-labelledby'))).not.toBeNull();
    }
  });

  it('does not claim two-key sequences as aria-keyshortcuts (UI-20)', () => {
    const el = render(ADMIN);
    expect(el.querySelector('[aria-keyshortcuts]')).toBeNull();
  });
});
