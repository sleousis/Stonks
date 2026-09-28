import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  Injectable,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter } from 'rxjs';

import { SessionService } from '../core/auth/session.service';
import { FeatureFlagsService } from '../core/features/feature-flags.service';
import { TicketCountService } from '../core/tickets/ticket-count.service';
import {
  type FoldingGroup,
  NAV_GROUPS,
  NAV_GROUPS_OPEN_BY_DEFAULT,
  NAV_ITEMS,
  type NavItem,
  groupForUrl,
  navItemVisible,
  navViewer,
} from './nav-items';

/** Where the open groups are kept between visits (a per-browser convenience). */
export const NAV_GROUPS_STORAGE_KEY = 'stonks.nav.groups';

/**
 * Which folding groups are open. Shared by the sidebar and the drawer, kept
 * in local storage, and the group that holds the current page always opens.
 */
@Injectable({ providedIn: 'root' })
export class NavGroupsState {
  private readonly router = inject(Router);
  readonly open = signal<ReadonlySet<FoldingGroup>>(readOpen());

  constructor() {
    this.reveal(this.router.url);
    this.router.events
      .pipe(
        filter((e) => e instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe((e) => this.reveal((e as NavigationEnd).urlAfterRedirects));
  }

  set(group: FoldingGroup, on: boolean): void {
    if (this.open().has(group) === on) return;
    const next = new Set(this.open());
    if (on) next.add(group);
    else next.delete(group);
    this.open.set(next);
    try {
      localStorage.setItem(NAV_GROUPS_STORAGE_KEY, JSON.stringify([...next]));
    } catch {
      // Storage blocked: the choice lasts for this page view.
    }
  }

  private reveal(url: string): void {
    const group = groupForUrl(url);
    if (group && (NAV_GROUPS as readonly string[]).includes(group)) {
      this.set(group as FoldingGroup, true);
    }
  }
}

function readOpen(): ReadonlySet<FoldingGroup> {
  try {
    const raw = localStorage.getItem(NAV_GROUPS_STORAGE_KEY);
    if (raw) {
      const list = JSON.parse(raw) as unknown;
      if (Array.isArray(list)) {
        return new Set(
          list.filter((g): g is FoldingGroup => (NAV_GROUPS as readonly unknown[]).includes(g)),
        );
      }
    }
  } catch {
    // Unreadable or blocked: fall back to the defaults.
  }
  return new Set(NAV_GROUPS_OPEN_BY_DEFAULT);
}

/**
 * Main navigation (M1). About seven trader pages sit on top with nothing to
 * open: Today, Strategies, Orders, Approvals, Insights, Charts and
 * Notifications. Below them, folding groups: More (markets, trial results,
 * trade costs, the assistant when it is on), Advanced (the build tools and
 * the strategy review) and, for admins, System. Each group is a disclosure
 * that remembers whether it is open, and the one holding the current page
 * opens by itself. Account pages live in the account menu, pinned below.
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
        <details class="fold" [open]="isOpen(group.title)" (toggle)="onToggle(group.title, $event)">
          <summary class="group" [id]="idPrefix() + '-group-' + group.title">
            <span>{{ group.title }}</span>
            <span class="chev" aria-hidden="true"></span>
          </summary>
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
        </details>
      }
    </nav>
  `,
  styles: `
    nav {
      display: grid;
      gap: var(--space-1);
    }
    .group {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 32px;
      margin: var(--space-2) 0 2px;
      padding: 0 var(--space-3);
      border-radius: var(--radius-sm);
      font-size: var(--text-xs);
      font-weight: var(--weight-semibold);
      color: var(--color-ink-3);
      cursor: pointer;
      list-style: none;
      user-select: none;
    }
    .group::-webkit-details-marker {
      display: none;
    }
    .group:hover {
      background: var(--color-surface-2);
      color: var(--color-ink);
    }
    .chev {
      width: 0.45em;
      height: 0.45em;
      margin-right: 2px;
      border-right: 1.5px solid currentColor;
      border-bottom: 1.5px solid currentColor;
      transform: rotate(-45deg);
      transition: transform var(--dur-fast) var(--ease);
    }
    details[open] > .group .chev {
      transform: rotate(45deg);
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
      a,
      .group {
        min-height: var(--touch-min);
      }
      kbd {
        display: none;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .chev {
        transition: none;
      }
    }
  `,
})
export class Nav {
  readonly navigate = output<void>();
  /** Keeps ids unique: the sidebar and the drawer both render a nav. */
  readonly idPrefix = input('nav');
  private readonly viewer = navViewer(inject(SessionService), inject(FeatureFlagsService));
  private readonly folds = inject(NavGroupsState);

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

  protected isOpen(group: FoldingGroup): boolean {
    return this.folds.open().has(group);
  }

  protected onToggle(group: FoldingGroup, event: Event): void {
    this.folds.set(group, (event.target as HTMLDetailsElement).open);
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
