import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { ADMIN, TRADER } from '../../testing/auth-fixtures';
import { Nav } from './nav';

const VIEWER: MeView = { ...TRADER, role: 'viewer', scopes: ['read'] };

describe('Nav', () => {
  const me = signal<MeView | null>(null);
  const canRead = signal(true);
  const session = {
    me,
    canRead,
    isAdmin: () => me()?.role === 'admin',
    can: (p: Permission) => allowed(me(), p),
  };

  beforeEach(() => {
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

  function labels(el: HTMLElement, list = 'ul') {
    return [...el.querySelectorAll(`${list} a span`)].map((s) => s.textContent);
  }

  function group(el: HTMLElement, title: string): string[] {
    const heading = [...el.querySelectorAll('.group')].find((g) => g.textContent === title);
    if (!heading) return [];
    return labels(el, `ul[aria-labelledby="${heading.id}"]`);
  }

  it('a trader sees Strategies and Orders without opening anything (UX-10)', () => {
    const el = render(TRADER);
    expect(el.querySelector('details')).toBeNull();
    expect(labels(el, 'ul[aria-label="Trading"]')).toEqual([
      'Today',
      'Strategies',
      'Orders',
      'Charts',
      'Watchlists',
      'Insights',
      'Notifications',
    ]);
    expect(group(el, 'Research')).toEqual([
      'Paper trading',
      'Leaderboard',
      'Studio',
      'Lab',
      'Go live',
      'Assistant',
    ]);
    expect(group(el, 'System')).toEqual([]);
    expect(el.textContent).not.toContain('Advanced');
  });

  it('a viewer does not see Schedule or Data quality, nor the build tools', () => {
    const el = render(VIEWER);
    const all = labels(el);
    expect(all).not.toContain('Schedule');
    expect(all).not.toContain('Data quality');
    expect(all).not.toContain('Users');
    expect(all).not.toContain('Studio');
    expect(all).not.toContain('Lab');
    // Viewers may still read strategies, paper trading and the leaderboard.
    expect(all).toContain('Strategies');
    expect(group(el, 'Research')).toEqual(['Paper trading', 'Leaderboard', 'Go live', 'Assistant']);
  });

  it('gives admins the System group, with Overview, Halts and Users', () => {
    const el = render(ADMIN);
    expect(group(el, 'System')).toEqual([
      'Overview',
      'Health',
      'Schedule',
      'Data',
      'Data quality',
      'Universes',
      'Halts',
      'Users',
    ]);
  });

  it('keeps account pages out of the main nav (they sit in the account menu)', () => {
    const all = labels(render(ADMIN));
    for (const label of ['Profile', 'Settings', 'Broker connections', 'Glossary', 'Trade costs']) {
      expect(all).not.toContain(label);
    }
  });

  it('shows everything under open reads (dev, nobody signed in)', () => {
    const el = render(null);
    expect(group(el, 'System')).toContain('Schedule');
  });

  it('points Paper trading at /paper', () => {
    const el = render(TRADER);
    const link = [...el.querySelectorAll('a')].find((a) => a.textContent?.includes('Paper'))!;
    expect(link.getAttribute('href')).toBe('/paper');
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
