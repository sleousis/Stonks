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
 * The in-app feed, newest first: everything the server sent the trader,
 * whatever the push, email and webhook settings are. Unread items carry a
 * dot and the word "Unread"; a deep link opens the page it is about.
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
  ],
  template: `
    <section class="panel" aria-labelledby="feed-title">
      <div class="panel-head head">
        <h2 id="feed-title">
          Feed
          @if (unread() > 0) {
            <span class="count muted">{{ unread() }} unread</span>
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
      </div>
      <div class="note-row"><app-permission-note permission="notifications.manage" /></div>

      @if (feed.error(); as err) {
        <app-error-state
          title="Could not load notifications"
          [error]="err"
          (retry)="feed.reload()"
        />
      } @else if (!feed.hasValue()) {
        <app-loading-state label="Loading notifications" [rows]="5" />
      } @else if (items().length === 0) {
        @if (unreadOnly()) {
          <app-empty-state
            title="Nothing unread"
            message="You are all caught up. Choose All to see older notifications."
          >
            <button type="button" class="btn" (click)="setShow('all')">Show all</button>
          </app-empty-state>
        } @else {
          <app-empty-state
            title="No notifications yet"
            message="Signals from the strategies you follow, order fills, risk alerts and system news land here. They stay here even when push is off or you missed one on your phone."
          >
            <a class="btn" routerLink="/strategies">Find strategies to follow</a>
          </app-empty-state>
        }
      } @else {
        <ul class="feed">
          @for (n of items(); track n.id) {
            <li [class.unread]="!n.read_at">
              <div class="meta">
                @if (!n.read_at) {
                  <span class="unread-tag"><span class="dot" aria-hidden="true"></span>Unread</span>
                }
                <span class="category">{{ category(n.category) }}</span>
                @if (n.level !== 'info') {
                  <app-status-pill
                    [status]="n.level"
                    [tone]="pill(n.level).tone"
                    [label]="pill(n.level).label"
                  />
                }
                <time
                  class="when muted"
                  [attr.datetime]="n.created_at"
                  [title]="exact(n.created_at)"
                >
                  {{ ago(n.created_at) }}
                </time>
              </div>
              <p class="title">{{ n.title }}</p>
              @if (n.message) {
                <p class="message">{{ n.message }}</p>
              }
              <div class="actions">
                @if (link(n); as href) {
                  <a class="btn btn-ghost open" [routerLink]="href" (click)="opened(n)">
                    Open<span class="visually-hidden">: {{ n.title }}</span>
                  </a>
                }
                @if (!n.read_at) {
                  <button
                    type="button"
                    class="btn btn-ghost"
                    [disabled]="busy() || !canManage()"
                    (click)="markOne(n)"
                  >
                    Mark read<span class="visually-hidden">: {{ n.title }}</span>
                  </button>
                }
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
    :host {
      display: block;
      min-width: 0;
    }
    .head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .count {
      margin-left: var(--space-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-regular);
    }
    .tools {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .note-row {
      padding: 0 var(--space-4);
    }
    .feed {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    li {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
      padding: var(--space-3) var(--space-4);
      border-bottom: 1px solid var(--color-border);
      border-left: 3px solid transparent;
    }
    li.unread {
      border-left-color: var(--color-primary);
      background: var(--color-surface-2);
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1) var(--space-2);
      font-size: var(--text-xs);
    }
    .unread-tag {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-weight: var(--weight-semibold);
      color: var(--color-primary);
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
    }
    .category {
      color: var(--color-ink-2);
      font-weight: var(--weight-medium);
    }
    .when {
      margin-left: auto;
      white-space: nowrap;
    }
    .title {
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    li:not(.unread) .title {
      font-weight: var(--weight-medium);
    }
    .message {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .actions:empty {
      display: none;
    }
    .more {
      display: flex;
      justify-content: center;
      padding: var(--space-3) var(--space-4);
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
