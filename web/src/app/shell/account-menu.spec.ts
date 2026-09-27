import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { TRADER } from '../../testing/auth-fixtures';
import { AccountMenu } from './account-menu';

describe('AccountMenu', () => {
  const me = signal<MeView | null>(TRADER);
  const session = {
    me,
    canRead: () => true,
    isAdmin: () => me()?.role === 'admin',
    can: (p: Permission) => allowed(me(), p),
  };

  function render(user: MeView | null) {
    me.set(user);
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: SessionService, useValue: session }],
    });
    const fixture = TestBed.createComponent(AccountMenu);
    fixture.detectChanges();
    return fixture;
  }

  it('sits behind the name and role, with the account pages and Sign out (UX-10)', () => {
    const fixture = render(TRADER);
    const el = fixture.nativeElement as HTMLElement;
    const summary = el.querySelector('summary')!;
    expect(summary.textContent).toContain('Ann');
    expect(summary.textContent).toContain('Trader');
    expect(summary.getAttribute('aria-label')).toBe('Ann, Trader. Account menu');
    const items = [
      ...el.querySelectorAll('ul[aria-label="Account"] a span, ul[aria-label="Account"] button'),
    ].map((e) => e.textContent?.trim());
    expect(items).toEqual([
      'Profile',
      'Settings',
      'Broker connections',
      'Get set up',
      'Glossary',
      'Sign out',
    ]);
    expect(el.querySelector('a[href="/welcome"]')).not.toBeNull();
  });

  it('closes after a choice and asks the shell to sign out', () => {
    const fixture = render(TRADER);
    const el = fixture.nativeElement as HTMLElement;
    const signOut = vi.fn();
    fixture.componentInstance.signOut.subscribe(signOut);
    const details = el.querySelector('details')!;
    details.open = true;
    el.querySelector<HTMLButtonElement>('button.sign-out')!.click();
    expect(signOut).toHaveBeenCalled();
    expect(details.open).toBe(false);
  });

  it('offers sign-in when nobody is signed in', () => {
    const el = render(null).nativeElement as HTMLElement;
    expect(el.querySelector('details')).toBeNull();
    expect(el.querySelector('a[href="/login"]')!.textContent).toContain('Sign in');
  });
});
