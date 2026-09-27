import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  output,
} from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

import { SessionService } from '../core/auth/session.service';
import { TicketCountService } from '../core/tickets/ticket-count.service';
import { NAV_GROUPS, NAV_ITEMS, type NavItem, navItemVisible, navViewer } from './nav-items';

/**
 * Main navigation (UX-10). The trader's pages sit on top with nothing to
 * open: Today, Strategies, Orders, Approvals, Charts, Watchlists, Insights and
 * Notifications. Research follows (paper trading, the leaderboard and the
 * build tools for those who can use them), then System for admins. Account
 * pages live in the account menu by the user's name.
 *
 * Approvals carries a badge with the tickets that wait (22.10). The number
 * is hidden from screen readers, and the link's label says it in words.
 */
const TICKETS = '/tickets';

@Component({
  selector: 'app-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav aria-label="Main">
      <ul aria-label="Trading">
        @for (item of main(); track item.path) {
          <li>
            <a
              [routerLink]="item.path"
              routerLinkActive="active"
              ariaCurrentWhenActive="page"
              [routerLinkActiveOptions]="{ exact: item.path === '/' }"
              [attr.aria-label]="spoken(item)"
              (click)="navigate.emit()"
            >
              <span>{{ item.label }}</span>
              @if (badge(item); as n) {
                <b class="count" aria-hidden="true">{{ n }}</b>
              }
              @if (item.key) {
                <kbd aria-hidden="true">g {{ item.key }}</kbd>
              }
            </a>
          </li>
        }
      </ul>
      @for (group of groups(); track group.title) {
        <p class="group" [id]="idPrefix() + '-group-' + group.title">{{ group.title }}</p>
        <ul [attr.aria-labelledby]="idPrefix() + '-group-' + group.title">
          @for (item of group.items; track item.path) {
            <li>
              <a
                [routerLink]="item.path"
                routerLinkActive="active"
                ariaCurrentWhenActive="page"
                (click)="navigate.emit()"
              >
                <span>{{ item.label }}</span>
                @if (item.key) {
                  <kbd aria-hidden="true">g {{ item.key }}</kbd>
                }
              </a>
            </li>
          }
        </ul>
      }
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
    .count {
      min-width: 20px;
      margin-left: auto;
      padding: 0 6px;
      border-radius: 999px;
      background: var(--color-primary);
      color: var(--color-primary-ink);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      line-height: 20px;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }
    .count + kbd {
      margin-left: var(--space-2);
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
      a {
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
  private readonly viewer = navViewer(inject(SessionService));

  private readonly visible = computed(() =>
    NAV_ITEMS.filter((i) => navItemVisible(i, this.viewer)),
  );
  protected readonly main = computed(() => this.visible().filter((i) => i.group === 'Main'));
  private readonly tickets = inject(TicketCountService);

  constructor() {
    const destroyRef = inject(DestroyRef);
    let watching = false;
    effect(() => {
      if (watching || !this.visible().some((i) => i.path === TICKETS)) return;
      watching = true;
      this.tickets.watch(destroyRef);
    });
  }

  /** The count shown on an item, or 0 for none. */
  protected badge(item: NavItem): number {
    return item.path === TICKETS ? this.tickets.waiting() : 0;
  }

  /** The link's accessible name when it carries a badge, else null (its text). */
  protected spoken(item: NavItem): string | null {
    const n = this.badge(item);
    return n > 0 ? `${item.label}, ${n} ${n === 1 ? 'ticket' : 'tickets'} waiting` : null;
  }
  protected readonly groups = computed(() =>
    NAV_GROUPS.map((title) => ({
      title,
      items: this.visible().filter((i: NavItem) => i.group === title),
    })).filter((g) => g.items.length > 0),
  );
}
