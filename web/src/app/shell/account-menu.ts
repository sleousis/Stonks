import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  inject,
  output,
  viewChild,
} from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

import type { Role } from '../api/models';
import { SessionService } from '../core/auth/session.service';
import { NAV_ITEMS, navItemVisible, navViewer } from './nav-items';

const ROLE_LABEL: Readonly<Record<Role, string>> = {
  viewer: 'Viewer',
  trader: 'Trader',
  admin: 'Admin',
};

/**
 * The account menu by the user's name, in the sidebar footer and the phone
 * drawer (UX-10): Profile, Settings, Broker connections, Get set up,
 * Glossary and Sign out. A disclosure, so it works without script tricks
 * and reads as a button to screen readers.
 */
@Component({
  selector: 'app-account-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    @if (session.me(); as me) {
      <details #menu class="account">
        <summary [attr.aria-label]="me.display_name + ', ' + role() + '. Account menu'">
          <span class="dot" aria-hidden="true"></span>
          <span class="who">
            <span class="who-name">{{ me.display_name }}</span>
            <span class="who-role">{{ role() }}</span>
          </span>
          <span class="chev" aria-hidden="true"></span>
        </summary>
        <ul aria-label="Account">
          @for (item of items(); track item.path) {
            <li>
              <a
                [routerLink]="item.path"
                routerLinkActive="active"
                ariaCurrentWhenActive="page"
                (click)="close(); navigate.emit()"
              >
                <span>{{ item.label }}</span>
                @if (item.key) {
                  <kbd aria-hidden="true">g {{ item.key }}</kbd>
                }
              </a>
            </li>
          }
          <li>
            <button type="button" class="sign-out" (click)="close(); signOut.emit()">
              Sign out
            </button>
          </li>
        </ul>
      </details>
    } @else {
      <a routerLink="/login" class="signed-out" (click)="navigate.emit()">
        <span class="dot" aria-hidden="true"></span>
        Read-only. Sign in
      </a>
    }
  `,
  styles: `
    :host {
      display: block;
    }
    summary {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      min-height: 40px;
      padding: var(--space-1) var(--space-3);
      border-radius: var(--radius-sm);
      font-size: var(--text-xs);
      color: var(--color-ink-2);
      cursor: pointer;
      list-style: none;
    }
    summary::-webkit-details-marker {
      display: none;
    }
    summary:hover,
    details[open] > summary {
      background: var(--color-surface-2);
    }
    .dot {
      flex: none;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      border: 1.5px solid var(--color-gain);
      background: var(--color-gain);
    }
    .signed-out .dot {
      border-color: var(--color-ink-3);
      background: none;
    }
    .who {
      display: grid;
      flex: 1;
      min-width: 0;
      line-height: var(--leading-tight);
    }
    .who-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--color-ink);
      font-size: var(--text-sm);
      font-weight: var(--weight-semibold);
    }
    .who-role {
      color: var(--color-ink-3);
    }
    .chev {
      width: 0.45em;
      height: 0.45em;
      border-right: 1.5px solid currentColor;
      border-bottom: 1.5px solid currentColor;
      transform: rotate(-135deg);
      transition: transform var(--dur-fast) var(--ease);
    }
    details[open] .chev {
      transform: rotate(45deg);
    }
    ul {
      display: grid;
      gap: 2px;
      margin: var(--space-1) 0 0;
      padding: 0 0 0 var(--space-3);
      list-style: none;
    }
    a,
    .sign-out {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      min-height: 34px;
      padding: 0 var(--space-3);
      border: 0;
      border-radius: var(--radius-sm);
      background: none;
      color: var(--color-ink-2);
      font: inherit;
      font-weight: var(--weight-medium);
      text-align: start;
      text-decoration: none;
      cursor: pointer;
    }
    a:hover,
    .sign-out:hover,
    a.active {
      background: var(--color-surface-2);
      color: var(--color-ink);
    }
    .signed-out {
      justify-content: flex-start;
      gap: var(--space-2);
      font-size: var(--text-xs);
    }
    kbd {
      font: inherit;
      font-size: var(--text-xs);
      color: var(--color-ink-3);
      opacity: 0;
    }
    a:hover kbd,
    a:focus-visible kbd {
      opacity: 1;
    }
    @media (pointer: coarse), (max-width: 767.98px) {
      summary,
      a,
      .sign-out {
        min-height: var(--touch-min);
      }
      kbd {
        display: none;
      }
    }
  `,
})
export class AccountMenu {
  /** A link was followed (the drawer closes). */
  readonly navigate = output<void>();
  readonly signOut = output<void>();

  protected readonly session = inject(SessionService);
  private readonly viewer = navViewer(this.session);
  private readonly menu = viewChild<ElementRef<HTMLDetailsElement>>('menu');

  protected readonly role = computed(() => {
    const me = this.session.me();
    if (!me) return '';
    return ROLE_LABEL[me.role] + (me.via === 'session' ? '' : ', API token');
  });

  protected readonly items = computed(() =>
    NAV_ITEMS.filter((i) => i.group === 'Account' && navItemVisible(i, this.viewer)),
  );

  protected close(): void {
    const el = this.menu()?.nativeElement;
    if (el) el.open = false;
  }
}
