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
import { NavigationEnd, Router, RouterLink } from '@angular/router';
import { filter } from 'rxjs';

import { SessionService } from '../core/auth/session.service';
import { FeatureFlagsService } from '../core/features/feature-flags.service';
import { TicketCountService } from '../core/tickets/ticket-count.service';
import {
  NAV_ITEMS,
  type NavItem,
  type TabWorkspace,
  WORKSPACES,
  type Workspace,
  itemForUrl,
  navItemVisible,
  navViewer,
  workspacesForUrl,
} from './nav-items';

const TICKETS = '/tickets';
/** The account pages in tab order. */
const ACCOUNT_ORDER = ['/profile', '/settings', '/notifications', '/welcome', '/help/glossary'];

/** A workspace with the pages this person may see in it. */
export interface WorkspaceView {
  name: TabWorkspace;
  blurb: string;
  items: readonly NavItem[];
}

/** A row of tabs: a workspace's pages, or the account pages. */
export interface TabView {
  name: Workspace;
  blurb: string;
  items: readonly NavItem[];
}

/**
 * The navigation's state, shared by the workspace switcher, the tabs, the
 * phone bottom nav and the More sheet: which pages this person sees, the
 * page on screen and its workspace. The active workspace is the one that
 * holds the current route. On an account page (Profile, Settings) the last
 * workspace stays for the switcher's grid, while the tab row shows the
 * account pages and the switcher reads "Account".
 */
@Injectable({ providedIn: 'root' })
export class NavState {
  private readonly router = inject(Router);
  private readonly viewer = navViewer(inject(SessionService), inject(FeatureFlagsService));
  private readonly tickets = inject(TicketCountService);

  /** The address on screen, without query or fragment. */
  readonly url = signal(this.router.url);
  private readonly lastTab = signal<TabWorkspace>('Trade');

  /** The pages this person may see, in nav order. */
  readonly visible = computed(() => NAV_ITEMS.filter((i) => navItemVisible(i, this.viewer)));

  /** The workspaces with at least one visible page, in switcher order. */
  readonly workspaces = computed<WorkspaceView[]>(() =>
    WORKSPACES.map((ws) => ({
      ...ws,
      items: this.visible().filter((i) => i.workspace === ws.name),
    })).filter((ws) => ws.items.length > 0),
  );

  /** The account pages (the avatar menu). */
  readonly accountItems = computed(() => this.visible().filter((i) => i.workspace === 'Account'));

  /** The nav item for the page on screen (the longest matching path). */
  readonly activeItem = computed(() => itemForUrl(this.url(), this.visible()));

  /** The workspace of the page on screen, or the last one for account pages. */
  readonly current = computed<TabWorkspace>(() => {
    const names = this.workspaces().map((w) => w.name);
    const found = workspacesForUrl(this.url(), this.visible()).find(
      (w): w is TabWorkspace => w !== 'Account' && names.includes(w as TabWorkspace),
    );
    if (found) return found;
    const last = this.lastTab();
    return names.includes(last) ? last : (names[0] ?? 'Trade');
  });

  /** The current workspace with its pages. */
  readonly currentView = computed(
    () => this.workspaces().find((w) => w.name === this.current()) ?? null,
  );

  /** Is the page on screen an account page (Profile, Settings, Help)? */
  readonly onAccount = computed(() => this.activeItem()?.workspace === 'Account');

  /** The switcher's label: the workspace on screen, or Account. */
  readonly label = computed<Workspace>(() => (this.onAccount() ? 'Account' : this.current()));

  /** The tabs in the chrome bar: the current workspace, or the account pages. */
  readonly tabView = computed<TabView | null>(() => {
    if (!this.onAccount()) return this.currentView();
    const rank = (i: NavItem) => {
      const at = ACCOUNT_ORDER.indexOf(i.path);
      return at < 0 ? ACCOUNT_ORDER.length : at;
    };
    const items = [...this.accountItems()].sort((a, b) => rank(a) - rank(b));
    return { name: 'Account', blurb: 'Your profile, settings and help', items };
  });

  constructor() {
    this.router.events
      .pipe(
        filter((e) => e instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe((e) => this.url.set((e as NavigationEnd).urlAfterRedirects));
    effect(() => this.lastTab.set(this.current()));
    const destroyRef = inject(DestroyRef);
    let watching = false;
    effect(() => {
      if (watching || !this.visible().some((i) => i.path === TICKETS)) return;
      watching = true;
      this.tickets.watch(destroyRef);
    });
  }

  /** Is `item` the page on screen? */
  isActive(item: NavItem): boolean {
    const active = this.activeItem();
    return !!active && active.path === item.path && active.workspace === item.workspace;
  }

  /** The count shown on an item, or 0 for none. */
  badge(item: NavItem): number {
    return item.path === TICKETS ? this.tickets.waiting() : 0;
  }

  /** The link's accessible name when it carries a badge, else null (its text). */
  spoken(item: NavItem): string | null {
    const n = this.badge(item);
    return n > 0 ? `${item.label}, ${n} ${n === 1 ? 'ticket' : 'tickets'} waiting` : null;
  }
}

/**
 * Every workspace and its pages as a list of links: the phone More sheet.
 * The page on screen carries aria-current. Approvals carries a badge with
 * the tickets that wait (22.10): the number is hidden from screen readers
 * and the link's label says it in words.
 */
@Component({
  selector: 'app-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <nav class="nav" aria-label="All pages">
      @for (ws of state.workspaces(); track ws.name) {
        <section class="group" [attr.aria-labelledby]="idPrefix() + '-ws-' + ws.name">
          <h2 class="group-title" [id]="idPrefix() + '-ws-' + ws.name">{{ ws.name }}</h2>
          <ul>
            @for (item of ws.items; track item.path) {
              <li>
                <a
                  [routerLink]="item.path"
                  [class.active]="state.isActive(item)"
                  [attr.aria-current]="state.isActive(item) ? 'page' : null"
                  [attr.aria-label]="state.spoken(item)"
                  (click)="navigate.emit()"
                >
                  <span>{{ item.label }}</span>
                  @if (state.badge(item); as n) {
                    <b class="count-badge" aria-hidden="true">{{ n }}</b>
                  }
                </a>
              </li>
            }
          </ul>
        </section>
      }
    </nav>
  `,
  styles: `
    .nav {
      display: grid;
      gap: var(--space-6);
    }
    .group-title {
      padding: 0 0 var(--space-2);
      border-bottom: 2px solid var(--rule-strong);
      font-size: var(--text-lead);
      font-weight: var(--weight-bold);
    }
    ul {
      display: grid;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li + li {
      border-top: 1px solid var(--rule);
    }
    a {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      min-height: var(--touch-min);
      color: var(--ink-2);
      font-weight: var(--weight-medium);
      text-decoration: none;
    }
    a:hover {
      color: var(--ink);
    }
    a.active {
      color: var(--accent);
      font-weight: var(--weight-semibold);
    }
  `,
})
export class Nav {
  /** A link was followed (the sheet closes). */
  readonly navigate = output<void>();
  /** Keeps ids unique if two lists render. */
  readonly idPrefix = input('nav');
  protected readonly state = inject(NavState);
}
