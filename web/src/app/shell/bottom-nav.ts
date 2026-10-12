import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { SessionService } from '../core/auth/session.service';
import { PrivacyService } from '../core/privacy/privacy.service';
import { type ThemeMode, ThemeService } from '../core/theme/theme.service';
import { Nav, NavState } from './nav';

interface Tab {
  path: string;
  label: string;
  icon: string;
}

/** The four pages a trader opens most, plus More. */
const TABS: readonly Tab[] = [
  { path: '/', label: 'Today', icon: 'M3 4h14v13H3zM3 8h14M7 2.5v3M13 2.5v3' },
  { path: '/strategies', label: 'Strategies', icon: 'M3 14.5 7.5 10l3 3L17 6.5' },
  { path: '/orders', label: 'Orders', icon: 'M4 5h12M4 10h12M4 15h7' },
  { path: '/charts', label: 'Charts', icon: 'M4 16V9M8 16V5M12 16v-5M16 16V7' },
];

const THEMES: readonly { value: ThemeMode; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
];

/**
 * Phones only: the bottom nav (Today, Strategies, Orders, Charts, More) and
 * the More sheet, a full-height list of every workspace and its pages, the
 * account pages, the theme, Hide money amounts and Sign out. 64px plus the
 * safe area, 44px targets, the current page in accent with a 2px top bar.
 */
@Component({
  selector: 'app-bottom-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, Nav],
  template: `
    <nav class="bottom-nav" aria-label="Main">
      @for (t of tabs; track t.path) {
        <a
          class="tab"
          [routerLink]="t.path"
          [attr.aria-current]="activePath() === t.path ? 'page' : null"
        >
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6">
            <path [attr.d]="t.icon" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          {{ t.label }}
        </a>
      }
      <button
        type="button"
        class="tab"
        data-open-nav
        aria-haspopup="dialog"
        aria-controls="more-sheet"
        [attr.aria-expanded]="open()"
        [class.current]="!activePath()"
        (click)="show()"
      >
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6">
          <rect x="3" y="3" width="5.5" height="5.5" rx="1.2" />
          <rect x="11.5" y="3" width="5.5" height="5.5" rx="1.2" />
          <rect x="3" y="11.5" width="5.5" height="5.5" rx="1.2" />
          <rect x="11.5" y="11.5" width="5.5" height="5.5" rx="1.2" />
        </svg>
        More
      </button>
    </nav>

    <!-- Backdrop click closes; Escape (native dialog) and the close button cover keyboards. -->
    <!-- eslint-disable-next-line @angular-eslint/template/click-events-have-key-events, @angular-eslint/template/interactive-supports-focus -->
    <dialog
      #sheet
      id="more-sheet"
      class="more"
      aria-labelledby="more-title"
      (close)="open.set(false)"
      (click)="onBackdrop($event)"
    >
      <div class="more-head">
        <h2 id="more-title">All pages</h2>
        <button type="button" class="btn btn-ghost btn-icon" (click)="hide()">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="M4 4l8 8M12 4l-8 8" />
          </svg>
          <span class="visually-hidden">Close</span>
        </button>
      </div>
      <div class="more-body">
        <app-nav idPrefix="more" (navigate)="hide()" />
        @if (session.me()) {
          <section class="account" aria-labelledby="more-account">
            <h2 id="more-account" class="group-title">Account</h2>
            <ul>
              @for (item of state.accountItems(); track item.path) {
                <li>
                  <a
                    [routerLink]="item.path"
                    [attr.aria-current]="state.isActive(item) ? 'page' : null"
                    (click)="hide()"
                    >{{ item.label }}</a
                  >
                </li>
              }
            </ul>
            <div class="theme" role="radiogroup" aria-label="Theme">
              @for (t of themes; track t.value) {
                <button
                  type="button"
                  role="radio"
                  [attr.aria-checked]="theme.mode() === t.value"
                  (click)="theme.setMode(t.value)"
                >
                  {{ t.label }}
                </button>
              }
            </div>
            <button
              type="button"
              class="row-button"
              [attr.aria-pressed]="privacy.hidden()"
              (click)="privacy.toggle()"
            >
              Hide money amounts
              <span class="muted">{{ privacy.hidden() ? 'On' : 'Off' }}</span>
            </button>
            <button type="button" class="row-button" (click)="signOut()">Sign out</button>
          </section>
        }
      </div>
    </dialog>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: contents;
    }
    .tab {
      position: relative;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 3px;
      min-height: var(--touch-min);
      padding: 0;
      border: 0;
      background: none;
      color: var(--muted);
      font: inherit;
      font-size: var(--text-2xs);
      font-weight: var(--weight-label);
      text-decoration: none;
      cursor: pointer;
    }
    .tab svg {
      width: 22px;
      height: 22px;
    }
    .tab[aria-current='page'],
    .tab.current {
      color: var(--accent);
    }
    .tab[aria-current='page']::before,
    .tab.current::before {
      content: '';
      position: absolute;
      top: -1px;
      left: 25%;
      right: 25%;
      height: 2px;
      border-radius: 0 0 2px 2px;
      background: var(--accent);
    }
    .more {
      width: 100vw;
      max-width: 100vw;
      height: 100dvh;
      max-height: 100dvh;
      margin: 0;
      padding: 0;
      border: 0;
      background: var(--bg);
      color: var(--ink);
    }
    .more[open] {
      display: flex;
      flex-direction: column;
      animation: page-enter var(--dur-page) var(--ease-out);
    }
    .more::backdrop {
      background: var(--scrim);
    }
    .more-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: calc(var(--space-2) + env(safe-area-inset-top)) var(--space-2) var(--space-2)
        var(--space-4);
      border-bottom: 1px solid var(--rule);
    }
    .more-head h2 {
      font-size: var(--text-section);
    }
    .more-body {
      flex: 1;
      display: grid;
      align-content: start;
      gap: var(--space-6);
      padding: var(--space-5) var(--space-4) calc(var(--space-6) + env(safe-area-inset-bottom));
      overflow-y: auto;
      overscroll-behavior: contain;
    }
    .group-title {
      padding: 0 0 var(--space-2);
      border-bottom: 2px solid var(--rule-strong);
      font-size: var(--text-lead);
    }
    ul {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li + li {
      border-top: 1px solid var(--rule);
    }
    .account a,
    .row-button {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      min-height: var(--touch-min);
      padding: 0;
      border: 0;
      background: none;
      color: var(--ink-2);
      font: inherit;
      font-weight: var(--weight-medium);
      text-align: start;
      text-decoration: none;
      cursor: pointer;
    }
    .row-button {
      border-top: 1px solid var(--rule);
    }
    .account a[aria-current='page'] {
      color: var(--accent);
    }
    .theme {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin: var(--space-3) 0;
      padding: 3px;
      border-radius: var(--r-ctl);
      background: var(--sunk);
    }
    .theme button {
      min-height: 38px;
      border: 1px solid transparent;
      border-radius: var(--r-tag);
      background: none;
      color: var(--ink-2);
      font-weight: var(--weight-label);
    }
    .theme button[aria-checked='true'] {
      border-color: var(--accent-line);
      background: var(--raise);
      color: var(--accent);
    }
    @include bp.from-tablet {
      .more {
        display: none;
      }
    }
  `,
})
export class BottomNav {
  protected readonly state = inject(NavState);
  protected readonly session = inject(SessionService);
  protected readonly theme = inject(ThemeService);
  protected readonly privacy = inject(PrivacyService);
  private readonly sheet = viewChild.required<ElementRef<HTMLDialogElement>>('sheet');
  protected readonly tabs = TABS;
  protected readonly themes = THEMES;
  protected readonly open = signal(false);

  /** The bottom tab that holds the page on screen, or null (More). */
  protected readonly activePath = computed(() => {
    const url = this.state.url().split(/[?#]/)[0] || '/';
    const hit = TABS.find((t) =>
      t.path === '/' ? url === '/' : url === t.path || url.startsWith(t.path + '/'),
    );
    return hit?.path ?? null;
  });

  show(): void {
    const el = this.sheet().nativeElement;
    if (!el.open) el.showModal?.();
    this.open.set(true);
  }

  hide(): void {
    const el = this.sheet().nativeElement;
    if (el.open) el.close();
    this.open.set(false);
  }

  protected onBackdrop(event: MouseEvent): void {
    if (event.target === this.sheet().nativeElement) this.hide();
  }

  protected async signOut(): Promise<void> {
    this.hide();
    // logout() loads /login afresh, so no user's state outlives the session (UX-07).
    await this.session.logout();
  }
}
