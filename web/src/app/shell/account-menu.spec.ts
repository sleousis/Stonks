import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { TRADER } from '../../testing/auth-fixtures';
import { AccountMenu, initials } from './account-menu';

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

  function open(fixture: ReturnType<typeof render>): HTMLElement {
    const el = fixture.nativeElement as HTMLElement;
    el.querySelector<HTMLButtonElement>('button[data-account-button]')!.click();
    fixture.detectChanges();
    return el;
  }

  it('makes initials from the name', () => {
    expect(initials('Ada Admin')).toBe('AA');
    expect(initials('Ann')).toBe('A');
    expect(initials('  ')).toBe('?');
  });

  it('sits behind the avatar, with the account pages, the theme and Sign out (UX-10)', () => {
    const fixture = render(TRADER);
    const button = (fixture.nativeElement as HTMLElement).querySelector('button')!;
    expect(button.getAttribute('aria-label')).toBe('Ann, Trader. Account menu');
    expect(button.getAttribute('aria-expanded')).toBe('false');
    const el = open(fixture);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(el.querySelector('.who')!.textContent).toContain('Trader');
    const items = [...el.querySelectorAll('ul[aria-label="Account"] a span')].map((e) =>
      e.textContent?.trim(),
    );
    expect(items).toEqual(['Profile', 'Settings', 'Broker connections', 'Get set up', 'Help']);
    expect(el.querySelector('a[href="/welcome"]')).not.toBeNull();
    const themes = [...el.querySelectorAll('[role="radiogroup"] [role="radio"]')].map((b) =>
      b.textContent?.trim(),
    );
    expect(themes).toEqual(['System', 'Light', 'Dark']);
    expect(el.textContent).toContain('Hide money amounts');
  });

  it('closes after a choice and asks the shell to sign out', () => {
    const fixture = render(TRADER);
    const signOut = vi.fn();
    fixture.componentInstance.signOut.subscribe(signOut);
    const el = open(fixture);
    el.querySelector<HTMLButtonElement>('button.sign-out')!.click();
    fixture.detectChanges();
    expect(signOut).toHaveBeenCalled();
    expect(el.querySelector('#account-menu')).toBeNull();
  });

  it('offers sign-in when nobody is signed in', () => {
    const el = render(null).nativeElement as HTMLElement;
    expect(el.querySelector('button[data-account-button]')).toBeNull();
    expect(el.querySelector('a[href="/login"]')!.textContent).toContain('Sign in');
  });
});
