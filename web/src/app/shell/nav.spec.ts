import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { MeView } from '../api/models';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { type Feature, FeatureFlagsService } from '../core/features/feature-flags.service';
import { TicketCountService } from '../core/tickets/ticket-count.service';
import { ADMIN, TRADER } from '../../testing/auth-fixtures';
import { tick } from '../../testing/http';
import { Nav, NavState } from './nav';
import { WorkspaceMenu } from './workspace-menu';
import { WorkspaceTabs } from './workspace-tabs';

const VIEWER: MeView = { ...TRADER, role: 'viewer', scopes: ['read'] };

@Component({ selector: 'app-blank', template: '<h1>Page</h1>' })
class Blank {}

describe('navigation', () => {
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
    watch = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: '**', component: Blank }]),
        { provide: SessionService, useValue: session },
        { provide: TicketCountService, useValue: { waiting, watch } },
        { provide: FeatureFlagsService, useValue: { on: (f: Feature) => !off().has(f) } },
      ],
    });
  });

  function render(user: MeView | null) {
    me.set(user);
    const fixture = TestBed.createComponent(Nav);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function group(el: HTMLElement, title: string): string[] {
    const h = [...el.querySelectorAll<HTMLElement>('h2')].find((x) => x.textContent === title);
    if (!h) return [];
    return [...h.parentElement!.querySelectorAll('a span')].map((s) => s.textContent ?? '');
  }

  function all(el: HTMLElement): string[] {
    return [...el.querySelectorAll('a span')].map((s) => s.textContent ?? '');
  }

  describe('All pages (the More sheet)', () => {
    it('lists a trader the workspaces Trade, Research and Portfolio, in tab order', () => {
      const el = render(TRADER);
      expect([...el.querySelectorAll('h2')].map((h) => h.textContent)).toEqual([
        'Trade',
        'Research',
        'Portfolio',
      ]);
      expect(group(el, 'Trade')).toEqual([
        'Today',
        'Orders',
        'Approvals',
        'Charts',
        'Watchlists',
        'Calendar',
        'Trade costs',
        'Journal',
      ]);
      expect(group(el, 'Research')).toEqual([
        'Strategies',
        'Build a strategy',
        'Lab',
        'Trial results',
        'Leaderboard',
        'Screener',
        'Options',
        'Assistant',
      ]);
      expect(group(el, 'Portfolio')).toEqual([
        'Insights',
        'Going live',
        'Halts',
        'Broker connections',
      ]);
    });

    it('never links a trader to an admin page (F11)', () => {
      const hrefs = [...render(TRADER).querySelectorAll('a')].map((a) => a.getAttribute('href'));
      for (const path of ['/ops/schedule', '/universes', '/data', '/health', '/go-live']) {
        expect(hrefs).not.toContain(path);
      }
    });

    it('hides features the server has off (F42, AO-11)', () => {
      off.set(new Set(['assistant', 'options', 'intraday', 'models']));
      const el = render(ADMIN);
      expect(all(el)).not.toContain('Assistant');
      expect(all(el)).not.toContain('Options');
      expect(group(el, 'System')).not.toContain('Intraday engine');
      expect(group(el, 'System')).not.toContain('Model versions');
    });

    it('a viewer does not see Schedule or Data quality, nor the build tools', () => {
      const labels = all(render(VIEWER));
      for (const hidden of ['Schedule', 'Data quality', 'Users', 'Build a strategy', 'Lab']) {
        expect(labels).not.toContain(hidden);
      }
      expect(labels).not.toContain('Approvals');
      expect(labels).toContain('Strategies');
    });

    it('gives admins the System workspace, with Halts once', () => {
      const el = render(ADMIN);
      expect(group(el, 'System')).toEqual([
        'Dashboard',
        'Health',
        'Intraday engine',
        'Schedule',
        'Data',
        'Data quality',
        'Universes',
        'Model versions',
        'Halts',
        'Users',
      ]);
      expect(all(el).filter((l) => l === 'Halts')).toHaveLength(1);
    });

    it('gives admins Strategy review in Research, after Trial results', () => {
      const research = group(render(ADMIN), 'Research');
      expect(research.indexOf('Strategy review')).toBe(research.indexOf('Trial results') + 1);
    });

    it('shows everything under open reads (dev, nobody signed in)', () => {
      expect(group(render(null), 'System')).toContain('Schedule');
    });

    it('badges Approvals with the tickets that wait, with a spoken label (22.10)', () => {
      waiting.set(3);
      const link = render(TRADER).querySelector<HTMLAnchorElement>('a[href="/tickets"]')!;
      expect(link.querySelector('.count-badge')?.textContent?.trim()).toBe('3');
      expect(link.querySelector('.count-badge')?.getAttribute('aria-hidden')).toBe('true');
      expect(link.getAttribute('aria-label')).toBe('Approvals, 3 tickets waiting');
      expect(watch).toHaveBeenCalled();
    });

    it('shows no badge when nothing waits', () => {
      const link = render(TRADER).querySelector<HTMLAnchorElement>('a[href="/tickets"]')!;
      expect(link.querySelector('.count-badge')).toBeNull();
      expect(link.getAttribute('aria-label')).toBeNull();
    });

    it('does not poll tickets for a viewer, who has no Approvals item', () => {
      render(VIEWER);
      expect(watch).not.toHaveBeenCalled();
    });
  });

  describe('workspace tabs and switcher', () => {
    async function go(url: string) {
      await TestBed.inject(Router).navigateByUrl(url);
      await tick();
    }

    it('shows the tabs of the workspace that holds the page, marking the page', async () => {
      me.set(TRADER);
      const fixture = TestBed.createComponent(WorkspaceTabs);
      await go('/strategies/momentum_1');
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;
      expect(el.querySelector('nav')!.getAttribute('aria-label')).toBe('Research pages');
      const current = el.querySelector('a[aria-current="page"]')!;
      expect(current.textContent).toContain('Strategies');
      await go('/orders/fills');
      fixture.detectChanges();
      expect(el.querySelector('nav')!.getAttribute('aria-label')).toBe('Trade pages');
      expect(el.querySelector('a[aria-current="page"]')!.textContent).toContain('Orders');
    });

    it('hints the go-to shortcut on tabs and switcher links (old nav.ts:136)', async () => {
      me.set(TRADER);
      const tabs = TestBed.createComponent(WorkspaceTabs);
      await go('/strategies');
      tabs.detectChanges();
      const tab = [...(tabs.nativeElement as HTMLElement).querySelectorAll('a.tab')].find((a) =>
        a.textContent?.includes('Strategies'),
      )!;
      expect(tab.getAttribute('title')).toMatch(/^Shortcut: g \w$/);
      const menu = TestBed.createComponent(WorkspaceMenu);
      menu.detectChanges();
      const el = menu.nativeElement as HTMLElement;
      el.querySelector<HTMLButtonElement>('button[data-open-nav]')!.click();
      menu.detectChanges();
      const hints = [...el.querySelectorAll('.pages kbd')].map((k) => k.textContent?.trim());
      expect(hints.length).toBeGreaterThan(0);
      expect(hints.every((h) => /^g \w$/.test(h ?? ''))).toBe(true);
    });

    it('keeps the last workspace on an account page', async () => {
      me.set(TRADER);
      const state = TestBed.inject(NavState);
      await go('/insights');
      TestBed.tick();
      expect(state.current()).toBe('Portfolio');
      await go('/profile');
      TestBed.tick();
      expect(state.current()).toBe('Portfolio');
    });

    it('opens the switcher with every workspace, the third spanning for a trader', async () => {
      me.set(TRADER);
      const fixture = TestBed.createComponent(WorkspaceMenu);
      await go('/');
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;
      const button = el.querySelector<HTMLButtonElement>('button[data-open-nav]')!;
      expect(button.textContent).toContain('Trade');
      expect(button.getAttribute('aria-expanded')).toBe('false');
      button.click();
      fixture.detectChanges();
      expect(button.getAttribute('aria-expanded')).toBe('true');
      const cells = [...el.querySelectorAll('.cell')];
      expect(cells.map((c) => c.querySelector('.name')!.textContent)).toEqual([
        'Trade',
        'Research',
        'Portfolio',
      ]);
      expect(cells[0].classList).toContain('current');
      const research = cells[1].querySelector<HTMLAnchorElement>('a.head')!;
      expect(research.getAttribute('href')).toBe('/strategies');
      el.querySelector('.ws-menu')!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
      fixture.detectChanges();
      expect(el.querySelector('.ws-menu')).toBeNull();
    });
  });
});
