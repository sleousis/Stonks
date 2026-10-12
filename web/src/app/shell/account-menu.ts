import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  output,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { Role } from '../api/models';
import { SessionService } from '../core/auth/session.service';
import { PrivacyService } from '../core/privacy/privacy.service';
import { type ThemeMode, ThemeService } from '../core/theme/theme.service';
import { NAV_ITEMS, type NavItem, navItemVisible, navViewer } from './nav-items';

const ROLE_LABEL: Readonly<Record<Role, string>> = {
  viewer: 'Viewer',
  trader: 'Trader',
  admin: 'Admin',
};

/** The account menu's pages, in order. Notifications sit behind the bell. */
const MENU_PATHS = ['/profile', '/settings', '/connections', '/welcome', '/help/glossary'];

const THEMES: readonly { value: ThemeMode; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
];

/** "AA" from "Ada Admin", "A" from "Ann". */
export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters =
    parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] ?? '?')[0];
  return letters.toUpperCase();
}

/**
 * The avatar at the right of the chrome bar and its 280px sheet (UX-10):
 * the name and role, Profile, Settings, Broker connections, Get set up,
 * Help, the theme (System, Light, Dark), Hide money amounts and Sign out.
 * Escape and a click outside close it.
 */
@Component({
  selector: 'app-account-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  host: {
    '(document:click)': 'onDocumentClick($event)',
    '(keydown.escape)': 'close(true)',
  },
  template: `
    @if (session.me(); as me) {
      <button
        type="button"
        class="chrome-icon avatar-button"
        data-account-button
        aria-haspopup="dialog"
        aria-controls="account-menu"
        [attr.aria-expanded]="open()"
        [attr.aria-label]="me.display_name + ', ' + role() + '. Account menu'"
        (click)="toggle()"
      >
        <span class="avatar" aria-hidden="true">{{ letters() }}</span>
      </button>
      @if (open()) {
        <div id="account-menu" class="chrome-sheet account" role="dialog" aria-label="Account">
          <p class="who">
            <span class="who-name">{{ me.display_name }}</span>
            <span class="who-role">{{ role() }}</span>
          </p>
          <ul aria-label="Account">
            @for (item of items(); track item.path) {
              <li>
                <a [routerLink]="item.path" (click)="close(); navigate.emit()">
                  <span>{{ item.label }}</span>
                  @if (item.key) {
                    <kbd aria-hidden="true">g {{ item.key }}</kbd>
                  }
                </a>
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
            aria-keyshortcuts="h Alt+Shift+H"
            [attr.aria-pressed]="privacy.hidden()"
            (click)="privacy.toggle()"
          >
            Hide money amounts
            <span class="state">{{ privacy.hidden() ? 'On' : 'Off' }}</span>
          </button>
          <button type="button" class="row-button sign-out" (click)="close(); signOut.emit()">
            Sign out
          </button>
        </div>
      }
    } @else {
      <a routerLink="/login" class="signed-out" (click)="navigate.emit()">Read-only. Sign in</a>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: contents;
    }
    .avatar-button {
      margin-left: var(--space-1);
    }
    .account {
      right: calc(var(--gutter) - 8px);
      display: grid;
      gap: var(--space-1);
      width: 280px;
      max-width: calc(100vw - 16px);
      transform-origin: top right;
    }
    .who {
      display: grid;
      padding: var(--space-2) var(--space-2) var(--space-3);
      border-bottom: 1px solid var(--rule);
      line-height: var(--leading-tight);
    }
    .who-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: var(--weight-semibold);
    }
    .who-role {
      margin-top: 2px;
      color: var(--muted);
      font-size: var(--text-sm);
    }
    ul {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    a,
    .row-button {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      min-height: 36px;
      padding: 0 var(--space-2);
      border: 0;
      border-radius: var(--r-ctl);
      background: none;
      color: var(--ink-2);
      font: inherit;
      font-size: var(--text-md);
      font-weight: var(--weight-medium);
      text-align: start;
      text-decoration: none;
      cursor: pointer;
    }
    a:hover,
    .row-button:hover {
      background: var(--sunk);
      color: var(--ink);
    }
    .state {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .sign-out {
      border-top: 1px solid var(--rule);
      border-radius: 0 0 var(--r-ctl) var(--r-ctl);
    }
    kbd {
      opacity: 0;
    }
    a:hover kbd,
    a:focus-visible kbd {
      opacity: 1;
    }
    .theme {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin: var(--space-1) 0;
      padding: 3px;
      border-radius: var(--r-ctl);
      background: var(--sunk);
    }
    .theme button {
      min-height: 30px;
      border: 1px solid transparent;
      border-radius: var(--r-tag);
      background: none;
      color: var(--ink-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-label);
      cursor: pointer;
    }
    .theme button[aria-checked='true'] {
      border-color: var(--accent-line);
      background: var(--raise);
      color: var(--accent);
    }
    .signed-out {
      display: inline-flex;
      align-items: center;
      min-height: var(--touch-min);
      padding: 0 var(--space-2);
      color: var(--chrome-ink);
      font-size: var(--text-sm);
      white-space: nowrap;
    }
    @include bp.phone {
      .account {
        position: fixed;
        top: calc(52px + env(safe-area-inset-top));
        right: 8px;
      }
      a,
      .row-button,
      .theme button {
        min-height: var(--touch-min);
      }
      kbd {
        display: none;
      }
      .signed-out {
        max-width: 7rem;
        white-space: normal;
        line-height: 1.2;
      }
    }
  `,
})
export class AccountMenu {
  /** A link was followed. */
  readonly navigate = output<void>();
  readonly signOut = output<void>();

  protected readonly session = inject(SessionService);
  protected readonly theme = inject(ThemeService);
  protected readonly privacy = inject(PrivacyService);
  private readonly viewer = navViewer(this.session);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  protected readonly themes = THEMES;
  protected readonly open = signal(false);

  protected readonly role = computed(() => {
    const me = this.session.me();
    if (!me) return '';
    return ROLE_LABEL[me.role] + (me.via === 'session' ? '' : ', API token');
  });

  protected readonly letters = computed(() => initials(this.session.me()?.display_name ?? '?'));

  protected readonly items = computed(() =>
    MENU_PATHS.map((path) => NAV_ITEMS.find((i) => i.path === path)).filter(
      (i): i is NavItem => !!i && navItemVisible(i, this.viewer),
    ),
  );

  protected toggle(): void {
    if (this.open()) {
      this.close();
      return;
    }
    this.open.set(true);
    afterNextRender(
      () => this.host.nativeElement.querySelector<HTMLElement>('.account a')?.focus(),
      { injector: this.injector },
    );
  }

  close(returnFocus = false): void {
    if (!this.open()) return;
    this.open.set(false);
    if (returnFocus) this.host.nativeElement.querySelector<HTMLElement>('.avatar-button')?.focus();
  }

  protected onDocumentClick(event: MouseEvent): void {
    if (this.open() && !this.host.nativeElement.contains(event.target as Node)) this.close();
  }
}
