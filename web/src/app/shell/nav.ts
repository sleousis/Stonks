import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter, map } from 'rxjs';

import { SessionService } from '../core/auth/session.service';
import { NAV_GROUPS, NAV_ITEMS, type NavItem } from './nav-items';

const ADVANCED_KEY = 'stonks.navAdvanced';

/**
 * Main navigation. The trader's own pages sit on top; the rest folds under
 * "Advanced", closed by default for traders and viewers signed in with a
 * session, open for admins and in token or dev mode. The choice is
 * remembered per browser, and the fold opens on its own while one of its
 * pages is showing.
 */
@Component({
  selector: 'app-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav aria-label="Main">
      <ul aria-label="Your pages">
        @for (item of mine(); track item.path) {
          <li>
            <a
              [routerLink]="item.path"
              routerLinkActive="active"
              ariaCurrentWhenActive="page"
              [routerLinkActiveOptions]="{ exact: item.path === '/' }"
              (click)="navigate.emit()"
            >
              <span>{{ item.label }}</span>
              <kbd aria-hidden="true">g {{ item.key }}</kbd>
            </a>
          </li>
        }
      </ul>
      <details [open]="advancedOpen()" (toggle)="onToggle($event)">
        <summary>Advanced</summary>
        @for (group of groups; track group) {
          <p class="group" [id]="idPrefix() + '-group-' + group">{{ group }}</p>
          <ul [attr.aria-labelledby]="idPrefix() + '-group-' + group">
            @for (item of itemsIn(group); track item.path) {
              <li>
                <a
                  [routerLink]="item.path"
                  routerLinkActive="active"
                  ariaCurrentWhenActive="page"
                  (click)="navigate.emit()"
                >
                  <span>{{ item.label }}</span>
                  <kbd aria-hidden="true">g {{ item.key }}</kbd>
                </a>
              </li>
            }
          </ul>
        }
      </details>
    </nav>
  `,
  styles: `
    nav {
      display: grid;
      gap: var(--space-1);
    }
    .group {
      margin: var(--space-3) 0 var(--space-1);
      padding: 0 var(--space-3);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      color: var(--color-ink-3);
    }
    ul {
      display: grid;
      gap: 2px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    details {
      margin-top: var(--space-3);
    }
    summary {
      display: flex;
      align-items: center;
      min-height: 34px;
      padding: 0 var(--space-3);
      border-radius: var(--radius-sm);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      color: var(--color-ink-2);
      cursor: pointer;
      list-style: none;
    }
    summary::-webkit-details-marker {
      display: none;
    }
    summary::before {
      content: '';
      width: 0.45em;
      height: 0.45em;
      margin-right: var(--space-2);
      border-right: 1.5px solid currentColor;
      border-bottom: 1.5px solid currentColor;
      transform: rotate(-45deg);
      transition: transform var(--dur-fast) var(--ease);
    }
    details[open] > summary::before {
      transform: rotate(45deg);
    }
    summary:hover {
      background: var(--color-surface-2);
      color: var(--color-ink);
    }
    a {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 34px;
      padding: 0 var(--space-3);
      border-radius: var(--radius-sm);
      color: var(--color-ink-2);
      text-decoration: none;
      font-weight: var(--weight-medium);
    }
    a:hover {
      background: var(--color-surface-2);
      color: var(--color-ink);
    }
    a.active {
      background: var(--color-surface-2);
      color: var(--color-ink);
      font-weight: var(--weight-semibold);
    }
    a.active::before {
      content: '';
      position: absolute;
      left: 0;
      top: 7px;
      bottom: 7px;
      width: 3px;
      border-radius: 2px;
      background: var(--color-accent);
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
      a,
      summary {
        min-height: var(--touch-min);
      }
      kbd {
        display: none;
      }
    }
  `,
})
export class Nav {
  readonly navigate = output<void>();
  /** Keeps ids unique: the sidebar and the drawer both render a nav. */
  readonly idPrefix = input('nav');
  private readonly session = inject(SessionService);
  private readonly router = inject(Router);

  protected readonly groups = NAV_GROUPS;
  protected readonly mine = computed(() =>
    NAV_ITEMS.filter((i) => i.group === 'You' && (!i.adminOnly || this.session.isAdmin())),
  );

  private readonly url = toSignal(
    this.router.events.pipe(
      filter((e) => e instanceof NavigationEnd),
      map(() => this.router.url),
    ),
    { initialValue: this.router.url },
  );
  private readonly stored = signal<boolean | null>(readStored());

  /** Folded for traders and viewers on a session; open for admins, tokens and dev. */
  private readonly defaultOpen = computed(() => {
    const me = this.session.me();
    return !me || me.via !== 'session' || me.role === 'admin';
  });
  private readonly onAdvancedPage = computed(() => {
    const path = this.url().split(/[?#]/)[0];
    return NAV_ITEMS.some(
      (i) => i.group !== 'You' && (path === i.path || path.startsWith(i.path + '/')),
    );
  });
  protected readonly advancedOpen = computed(
    () => this.onAdvancedPage() || (this.stored() ?? this.defaultOpen()),
  );

  protected itemsIn(group: string): NavItem[] {
    return NAV_ITEMS.filter((i) => i.group === group);
  }

  protected onToggle(event: Event): void {
    const open = (event.target as HTMLDetailsElement).open;
    if (open === this.advancedOpen()) return;
    this.stored.set(open);
    try {
      localStorage.setItem(ADVANCED_KEY, open ? '1' : '0');
    } catch {
      // Storage blocked: the choice lasts until reload.
    }
  }
}

function readStored(): boolean | null {
  try {
    const v = localStorage.getItem(ADVANCED_KEY);
    return v === null ? null : v === '1';
  } catch {
    return null;
  }
}
