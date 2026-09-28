import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { type Feature, FeatureFlagsService } from '../core/features/feature-flags.service';
import { TicketCountService } from '../core/tickets/ticket-count.service';
import { ADMIN, TRADER } from '../../testing/auth-fixtures';
import { NAV_GROUPS_STORAGE_KEY, Nav } from './nav';

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

  const waiting = signal(0);
  const off = signal<ReadonlySet<Feature>>(new Set());
  let watch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    waiting.set(0);
    off.set(new Set());
    localStorage.removeItem(NAV_GROUPS_STORAGE_KEY);
    watch = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: SessionService, useValue: session },
        { provide: TicketCountService, useValue: { waiting, watch } },
        { provide: FeatureFlagsService, useValue: { on: (f: Feature) => !off().has(f) } },
      ],
    });
  });

  afterEach(() => localStorage.removeItem(NAV_GROUPS_STORAGE_KEY));

  function render(user: MeView | null) {
    me.set(user);
    const fixture = TestBed.createComponent(Nav);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function labels(el: HTMLElement, list = 'ul') {
    return [...el.querySelectorAll(`${list} a span`)].map((s) => s.textContent);
  }

  function heading(el: HTMLElement, title: string) {
    return [...el.querySelectorAll<HTMLElement>('summary.group')].find(
      (g) => g.textContent?.trim() === title,
    );
  }

  function group(el: HTMLElement, title: string): string[] {
    const h = heading(el, title);
    if (!h) return [];
    return labels(el, `ul[aria-labelledby="${h.id}"]`);
  }

  function isOpen(el: HTMLElement, title: string): boolean {
    return (heading(el, title)?.parentElement as HTMLDetailsElement | undefined)?.open ?? false;
  }

  it('gives a trader seven pages on top, the rest in folding groups (M1)', () => {
    const el = render(TRADER);
    expect(labels(el, 'ul[aria-label="Trading"]')).toEqual([
      'Today',
      'Strategies',
      'Orders',
      'Approvals',
      'Insights',
      'Charts',
      'Notifications',
    ]);
    expect(group(el, 'More')).toEqual([
      'Going live',
      'Watchlists',
      'Calendar',
      'Screener',
      'Trial results',
      'Leaderboard',
      'Trade costs',
      'Assistant',
    ]);
    expect(group(el, 'Advanced')).toEqual(['Studio', 'Lab', 'Options', 'Strategy review', 'Halts']);
    expect(group(el, 'System')).toEqual([]);
  });

  it('never links a trader to an admin page (F11)', () => {
    const hrefs = [...render(TRADER).querySelectorAll('a')].map((a) => a.getAttribute('href'));
    for (const path of ['/ops/schedule', '/universes', '/data', '/health']) {
      expect(hrefs).not.toContain(path);
    }
  });

  it('opens More at first and keeps Advanced folded, remembering a choice', () => {
    const el = render(TRADER);
    expect(isOpen(el, 'More')).toBe(true);
    expect(isOpen(el, 'Advanced')).toBe(false);
    const advanced = heading(el, 'Advanced')!.parentElement as HTMLDetailsElement;
    advanced.open = true;
    advanced.dispatchEvent(new Event('toggle'));
    expect(JSON.parse(localStorage.getItem(NAV_GROUPS_STORAGE_KEY)!)).toContain('Advanced');
  });

  it('hides the Assistant while the server has it off (F42)', () => {
    off.set(new Set(['assistant']));
    expect(group(render(TRADER), 'More')).not.toContain('Assistant');
  });

  it('a viewer does not see Schedule or Data quality, nor the build tools', () => {
    const el = render(VIEWER);
    const all = labels(el);
    expect(all).not.toContain('Schedule');
    expect(all).not.toContain('Data quality');
    expect(all).not.toContain('Users');
    expect(all).not.toContain('Studio');
    expect(all).not.toContain('Lab');
    expect(all).not.toContain('Approvals');
    // Viewers may still read strategies, trial results and the leaderboard.
    expect(all).toContain('Strategies');
    expect(group(el, 'Advanced')).toEqual(['Options', 'Strategy review']);
  });

  it('gives admins the System group, with Dashboard, Halts and Users', () => {
    const el = render(ADMIN);
    expect(group(el, 'System')).toEqual([
      'Dashboard',
      'Health',
      'Live engine',
      'Schedule',
      'Data',
      'Data quality',
      'Universes',
      'Model versions',
      'Halts',
      'Users',
    ]);
    expect(labels(el)).not.toContain('Overview');
    // Halts sits in System for admins, once.
    expect(labels(el).filter((l) => l === 'Halts')).toHaveLength(1);
    expect(group(el, 'Advanced')).not.toContain('Halts');
  });

  it('keeps account pages out of the main nav (they sit in the account menu)', () => {
    const all = labels(render(ADMIN));
    for (const label of ['Profile', 'Settings', 'Broker connections', 'Help']) {
      expect(all).not.toContain(label);
    }
  });

  it('shows everything under open reads (dev, nobody signed in)', () => {
    const el = render(null);
    expect(group(el, 'System')).toContain('Schedule');
  });

  it('badges Approvals with the tickets that wait, with a spoken label (22.10)', () => {
    waiting.set(3);
    const el = render(TRADER);
    const link = el.querySelector<HTMLAnchorElement>('a[href="/tickets"]')!;
    expect(link.querySelector('.count')?.textContent?.trim()).toBe('3');
    expect(link.querySelector('.count')?.getAttribute('aria-hidden')).toBe('true');
    expect(link.getAttribute('aria-label')).toBe('Approvals, 3 tickets waiting');
    expect(watch).toHaveBeenCalled();
  });

  it('shows no badge when nothing waits, and one ticket in the singular', () => {
    waiting.set(0);
    const el = render(TRADER);
    const link = el.querySelector<HTMLAnchorElement>('a[href="/tickets"]')!;
    expect(link.querySelector('.count')).toBeNull();
    expect(link.getAttribute('aria-label')).toBeNull();
    waiting.set(1);
    const again = render(TRADER).querySelector<HTMLAnchorElement>('a[href="/tickets"]')!;
    expect(again.getAttribute('aria-label')).toBe('Approvals, 1 ticket waiting');
  });

  it('does not poll tickets for a viewer, who has no Approvals item', () => {
    render(VIEWER);
    expect(watch).not.toHaveBeenCalled();
  });

  it('points Trial results at /paper', () => {
    const el = render(TRADER);
    const link = [...el.querySelectorAll('a')].find((a) => a.textContent?.includes('Trial'))!;
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
