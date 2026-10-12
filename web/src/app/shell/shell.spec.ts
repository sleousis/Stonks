import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { HaltView, MeView } from '../api/models';
import { provideApi } from '../api/provide-api';
import { type Permission, allowed } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { StepUpService } from '../core/auth/step-up.service';
import { ShortcutsService } from '../core/commands/shortcuts.service';
import { HaltStateService } from '../core/halts/halt-state.service';
import { TRADER } from '../../testing/auth-fixtures';
import { tick } from '../../testing/http';
import { Shell } from './shell';

@Component({ selector: 'app-today-stub', template: '<h1 tabindex="-1">Today</h1>' })
class TodayPage {}

@Component({ selector: 'app-orders-stub', template: '<h1 tabindex="-1">Orders</h1>' })
class OrdersPage {}

@Component({ selector: 'app-login-stub', template: '<p>Sign in</p>' })
class LoginPage {}

describe('Shell', () => {
  let fixture: ComponentFixture<Shell>;
  let el: HTMLElement;
  let router: Router;
  const me = signal<MeView | null>(TRADER);

  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  beforeEach(async () => {
    me.set(TRADER);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([
          { path: '', component: TodayPage },
          { path: 'orders', component: OrdersPage },
          { path: 'login', component: LoginPage, data: { bare: true } },
        ]),
        {
          provide: SessionService,
          useValue: {
            me,
            // No reads: the strip, the bell and the halt poller stay quiet.
            canRead: () => false,
            viaSession: () => true,
            isAdmin: () => me()?.role === 'admin',
            can: (p: Permission) => allowed(me(), p),
            csrfToken: () => null,
            load: () => Promise.resolve('signed-in'),
            logout: vi.fn().mockResolvedValue(undefined),
          },
        },
      ],
    });
    router = TestBed.inject(Router);
    fixture = TestBed.createComponent(Shell);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await router.navigateByUrl('/');
    await settle();
  });

  it('frames the page: chrome bar, workspace tabs, account menu, session rail and main', () => {
    const chrome = el.querySelector('header.chrome')!;
    expect(chrome.querySelector('app-workspace-menu button[data-open-nav]')).not.toBeNull();
    expect(chrome.querySelector('app-workspace-tabs nav')!.getAttribute('aria-label')).toBe(
      'Trade pages',
    );
    expect(
      chrome
        .querySelector('app-account-menu button[data-account-button]')!
        .getAttribute('aria-label'),
    ).toContain('Ann');
    expect(chrome.querySelector('app-session-strip')).not.toBeNull();
    expect(el.querySelector('app-bottom-nav nav.bottom-nav')).not.toBeNull();
    expect(el.querySelector('main#main h1')!.textContent).toBe('Today');
  });

  it('renders sign-in bare, without the frame', async () => {
    await router.navigateByUrl('/login');
    await settle();
    expect(el.querySelector('main.bare')).not.toBeNull();
    expect(el.querySelector('header.chrome')).toBeNull();
    expect(el.querySelector('app-bottom-nav')).toBeNull();
    expect(el.querySelector('app-session-strip')).toBeNull();
  });

  it('opens the More sheet from the bottom nav and closes it when a page is picked', async () => {
    const more = el.querySelector<HTMLButtonElement>('app-bottom-nav button[data-open-nav]')!;
    more.click();
    fixture.detectChanges();
    expect(more.getAttribute('aria-expanded')).toBe('true');
    expect(el.querySelector('#more-sheet app-nav')).not.toBeNull();
    el.querySelector<HTMLAnchorElement>('#more-sheet a[href="/orders"]')!.click();
    await settle();
    expect(more.getAttribute('aria-expanded')).toBe('false');
  });

  it("moves focus to the new page's heading after navigation", async () => {
    await router.navigateByUrl('/orders');
    await settle();
    expect(document.activeElement?.textContent).toBe('Orders');
  });

  it('Ctrl+K opens the deferred palette on the first press (UX-44)', async () => {
    expect(el.querySelector('app-command-palette')).toBeNull();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
    expect(TestBed.inject(ShortcutsService).paletteOpen()).toBe(true);
    // The palette asks for strategies and jobs as it opens, so wait by ticks, not whenStable.
    for (let i = 0; i < 20 && !el.querySelector('app-command-palette dialog'); i++) {
      await settle();
    }
    expect(el.querySelector('app-command-palette dialog')).not.toBeNull();
  });

  it('? opens the deferred cheat sheet on the first press (UX-44)', async () => {
    expect(el.querySelector('app-shortcut-help')).toBeNull();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: '?' }));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('app-shortcut-help #shortcuts-title')).not.toBeNull();
  });

  it('a step-up request loads the deferred prompt (UX-44)', async () => {
    expect(el.querySelector('app-step-up-dialog')).toBeNull();
    void TestBed.inject(StepUpService).prompt('Resume trading');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('app-step-up-dialog')!.textContent).toContain('Resume trading');
    TestBed.inject(StepUpService).cancel();
  });

  it('turns the rail red while a kill switch is on (UX-51)', () => {
    expect(el.querySelector('header.chrome')!.hasAttribute('data-tone')).toBe(false);
    TestBed.inject(HaltStateService).add({
      id: 1,
      kind: 'kill',
      scope: 'global',
      halt: 'all',
      active: true,
    } as HaltView);
    fixture.detectChanges();
    expect(el.querySelector('header.chrome')!.getAttribute('data-tone')).toBe('kill');
  });
});
