import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { FeedItemView } from '../../api/models';
import { NotificationsService } from '../../api/notifications.service';
import { SessionService } from '../../core/auth/session.service';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { NotificationFeedService, appLink } from '../../core/notify/notification-feed.service';
import { ToastService } from '../../core/notify/toast.service';
import { levelPill } from '../../shared/ui/alerts-panel';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { CategoryGlyph } from './category-glyph';

/** Items per page; the API allows up to 200. */
export const FEED_PAGE = 30;

const CATEGORY_LABELS: Record<string, string> = {
  signal: 'Signal',
  price_alert: 'Price alert',
  event_alert: 'Upcoming event',
  screen_alert: 'Screen alert',
  order: 'Order',
  risk: 'Risk',
  system: 'System',
};

/** "Signal", "Order", or the category itself with a capital. */
export function categoryLabel(category: string | null): string {
  if (!category) return 'General';
  return CATEGORY_LABELS[category] ?? category[0].toUpperCase() + category.slice(1);
}

/**
 * The inbox, newest first: everything the server sent the trader, whatever
 * the push, email and webhook settings are. Each row has its category's
 * glyph; unread rows carry a 3px accent edge and the word "Unread"; a deep
 * link opens the page it is about.
 */
@Component({
  selector: 'app-notification-feed',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    Segmented,
    StatusPill,
    PermissionNote,
    LoadingState,
    EmptyState,
    ErrorState,
    CategoryGlyph,
  ],
  template: `
    <section class="page-section" aria-labelledby="feed-title">
      <header class="section-head head">
        <h2 id="feed-title">
          Inbox
          @if (unread() > 0) {
            <span class="count">{{ unread() }} unread</span>
          }
        </h2>
        <div class="tools">
          <app-segmented
            label="Show"
            [options]="filters"
            [value]="show()"
            (valueChange)="setShow($event)"
          />
          <button
            type="button"
            class="btn"
            [disabled]="busy() || unread() === 0 || !canManage()"
            (click)="markAll()"
          >
            Mark all read
          </button>
        </div>
      </header>
      <app-permission-note permission="notifications.manage" />

      @if (feed.error(); as err) {
        <div class="section-body">
          <app-error-state
            title="Could not load notifications"
            [error]="err"
            (retry)="feed.reload()"
          />
        </div>
      } @else if (!feed.hasValue()) {
        <div class="section-body">
          <app-loading-state label="Loading notifications" [rows]="5" />
        </div>
      } @else if (items().length === 0) {
        <div class="section-body">
          @if (unreadOnly()) {
            <app-empty-state
              icon="check"
              tone="good"
              title="Nothing unread"
              message="You are all caught up. Choose All to see older notifications."
            >
              <button type="button" class="btn" (click)="setShow('all')">Show all</button>
            </app-empty-state>
          } @else {
            <app-empty-state
              icon="inbox"
              title="No notifications yet"
              message="Signals from the strategies you follow, order fills, risk alerts and system news land here. They stay here even when push is off or you missed one on your phone."
            >
              <a class="btn" routerLink="/strategies">Find strategies to follow</a>
            </app-empty-state>
          }
        </div>
      } @else {
        <ul class="feed">
          @for (n of items(); track n.id) {
            <li [class.unread]="!n.read_at">
              <app-category-glyph [category]="n.category" />
              <div class="text">
                <div class="meta">
                  <span class="category">{{ category(n.category) }}</span>
                  @if (!n.read_at) {
                    <span class="unread-tag">Unread</span>
                  }
                  @if (n.level !== 'info') {
                    <app-status-pill
                      [status]="n.level"
                      [tone]="pill(n.level).tone"
                      [label]="pill(n.level).label"
                    />
                  }
                </div>
                <p class="title">{{ n.title }}</p>
                @if (n.message) {
                  <p class="message">{{ n.message }}</p>
                }
              </div>
              <div class="actions">
                <time class="when num" [attr.datetime]="n.created_at" [title]="exact(n.created_at)">
                  {{ ago(n.created_at) }}
                </time>
                <span class="buttons">
                  @if (!n.read_at) {
                    <button
                      type="button"
                      class="btn btn-ghost btn-compact"
                      [disabled]="busy() || !canManage()"
                      (click)="markOne(n)"
                    >
                      Mark read<span class="visually-hidden">: {{ n.title }}</span>
                    </button>
                  }
                  @if (link(n); as href) {
                    <a class="btn btn-compact open" [routerLink]="href" (click)="opened(n)">
                      Open<span class="visually-hidden">: {{ n.title }}</span>
                    </a>
                  }
                </span>
              </div>
            </li>
          }
        </ul>
        @if (hasMore()) {
          <div class="more">
            <button type="button" class="btn" [disabled]="loadingMore()" (click)="loadOlder()">
              {{ loadingMore() ? 'Loading…' : 'Show older' }}
            </button>
          </div>
        }
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
      min-width: 0;
    }
    .tools {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .feed {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    /* One row: the category glyph, the text, then the time and actions. */
    li {
      display: grid;
      grid-template-columns: 32px minmax(0, 1fr) auto;
      gap: var(--space-1) var(--space-4);
      align-items: start;
      min-width: 0;
      padding: var(--space-4) var(--space-3) var(--space-4) var(--space-4);
      border-bottom: 1px solid var(--rule);
      border-left: 3px solid transparent;
    }
    /* Unread: a 3px accent edge and a semibold title. */
    li.unread {
      border-left-color: var(--accent);
    }
    .text {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-2);
      font-size: var(--text-xs);
    }
    .category {
      color: var(--muted);
      font-weight: var(--weight-medium);
    }
    .unread-tag {
      color: var(--accent);
      font-weight: var(--weight-semibold);
    }
    .title {
      max-width: 68ch;
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    li:not(.unread) .title {
      color: var(--ink-2);
      font-weight: var(--weight-medium);
    }
    .message {
      max-width: 68ch;
      color: var(--ink-2);
      font-size: var(--text-md);
      overflow-wrap: anywhere;
    }
    .actions {
      display: grid;
      justify-items: end;
      gap: var(--space-2);
    }
    .when {
      color: var(--muted);
      font-size: var(--text-xs);
    }
    .buttons {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: var(--space-2);
    }
    /* Phones: the actions move under the text. */
    @include bp.phone {
      li {
        grid-template-columns: 32px minmax(0, 1fr);
        padding: var(--space-3) 0 var(--space-3) var(--space-3);
      }
      .actions {
        grid-column: 2;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
      }
    }
    /* Touch: Mark read and Open keep a 44px target (old .text-btn and .open). */
    @include bp.phone {
      .buttons .btn {
        min-height: var(--touch-min);
      }
    }
    @include bp.coarse {
      .buttons .btn {
        min-height: var(--touch-min);
      }
    }
    .more {
      display: flex;
      justify-content: center;
      padding: var(--space-4) 0;
    }
  `,
})
export class NotificationFeed {
  private readonly api = inject(NotificationsService);
  private readonly counter = inject(NotificationFeedService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  protected readonly filters: readonly SegmentOption<'all' | 'unread'>[] = [
    { value: 'all', label: 'All' },
    { value: 'unread', label: 'Unread only' },
  ];
  protected readonly show = signal<'all' | 'unread'>('all');
  protected readonly unreadOnly = computed(() => this.show() === 'unread');
  /** Bumped when the filter changes, so an older page from the last filter is dropped (UX-33). */
  private filterEpoch = 0;
  protected readonly feed = resource({
    params: () => ({ unread_only: this.unreadOnly(), limit: FEED_PAGE }),
    loader: ({ params }) => this.api.feed(params),
  });
  /** The first page plus any older pages loaded since. */
  protected readonly items = linkedSignal<FeedItemView[]>(() =>
    this.feed.hasValue() ? this.feed.value().items : [],
  );
  protected readonly hasMore = linkedSignal(
    () => this.feed.hasValue() && this.feed.value().items.length >= FEED_PAGE,
  );
  protected readonly unread = this.counter.unread;
  protected readonly canManage = computed(() => this.session.can('notifications.manage'));
  protected readonly busy = signal(false);
  protected readonly loadingMore = signal(false);
  protected readonly pill = levelPill;
  protected readonly category = categoryLabel;

  constructor() {
    // Each load carries the server's unread count: keep the bell in step.
    effect(() => {
      if (this.feed.hasValue()) this.counter.set(this.feed.value().unread_count);
    });
  }

  protected setShow(value: 'all' | 'unread'): void {
    if (value === this.show()) return;
    this.filterEpoch += 1;
    this.show.set(value);
  }

  protected link(n: FeedItemView): string | null {
    return appLink(n.deep_link);
  }

  protected ago(value: string): string {
    return formatAgo(value);
  }

  protected exact(value: string): string {
    return formatDateTime(value);
  }

  protected async loadOlder(): Promise<void> {
    const last = this.items().at(-1);
    if (!last) return;
    const epoch = this.filterEpoch;
    this.loadingMore.set(true);
    try {
      const page = await this.api.feed({
        unread_only: this.unreadOnly(),
        limit: FEED_PAGE,
        before_id: last.id,
      });
      if (epoch !== this.filterEpoch) return;
      this.items.update((items) => [...items, ...page.items]);
      this.hasMore.set(page.items.length >= FEED_PAGE);
      this.counter.set(page.unread_count);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.loadingMore.set(false);
    }
  }

  protected async markOne(n: FeedItemView): Promise<void> {
    if (await this.mark([n.id])) this.toasts.success('Marked read.');
  }

  protected async markAll(): Promise<void> {
    if (await this.mark()) this.toasts.success('Marked all read.');
  }

  /** Opening an unread item's link also marks it read, quietly. */
  protected opened(n: FeedItemView): void {
    if (!n.read_at && this.canManage()) void this.mark([n.id]);
  }

  private async mark(ids?: number[]): Promise<boolean> {
    this.busy.set(true);
    try {
      await this.counter.markRead(ids);
      const now = new Date().toISOString();
      const hit = (n: FeedItemView) => !ids || ids.includes(n.id);
      this.items.update((items) =>
        items.map((n) => (hit(n) && !n.read_at ? { ...n, read_at: now } : n)),
      );
      return true;
    } catch {
      // The error interceptor already showed the API's message.
      return false;
    } finally {
      this.busy.set(false);
    }
  }
}
