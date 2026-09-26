import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { FeedItemView } from '../../api/models';
import { NotificationsService } from '../../api/notifications.service';
import { formatAgo } from '../../core/format/format';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

const DAY_MS = 24 * 60 * 60 * 1000;
const FEED_LIMIT = 100;

/**
 * Signals from the last 24 hours, newest first. Ticks run after the close,
 * so "today" is a rolling day: last night's signals still count this morning.
 */
export function todaysSignals(items: readonly FeedItemView[], now = new Date()): FeedItemView[] {
  const since = now.getTime() - DAY_MS;
  return items.filter((i) => i.category === 'signal' && new Date(i.created_at).getTime() >= since);
}

/** A deep link the router can open (same-app paths only). */
export function appLink(link: string | null): string | null {
  return link && link.startsWith('/') && !link.startsWith('//') ? link : null;
}

/** Today's signals, from the notifications feed. */
@Component({
  selector: 'app-signals-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, LoadingState, EmptyState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="home-signals">
      <div class="panel-head">
        <h2 id="home-signals">Today's signals</h2>
        @if (unread().length) {
          <button type="button" class="btn btn-ghost" [disabled]="marking()" (click)="markRead()">
            Mark all read
          </button>
        }
      </div>
      @if (feed.error(); as err) {
        <app-error-state title="Could not load signals" [error]="err" (retry)="feed.reload()" />
      } @else if (!feed.hasValue()) {
        <app-loading-state label="Loading signals" [rows]="3" />
      } @else if (signals().length === 0) {
        <app-empty-state
          title="No signals today"
          message="Signals from the strategies you follow show here after each daily run."
        />
      } @else {
        <ul class="signals">
          @for (s of signals(); track s.id) {
            <li [class.unread]="!s.read_at">
              @if (!s.read_at) {
                <span class="dot" aria-hidden="true"></span>
                <span class="visually-hidden">New.</span>
              }
              <div class="text">
                @if (link(s); as href) {
                  <a [routerLink]="href" class="title">{{ s.title }}</a>
                } @else {
                  <span class="title">{{ s.title }}</span>
                }
                <p class="message">{{ s.message }}</p>
              </div>
              <span class="when muted">{{ ago(s.created_at) }}</span>
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .signals {
      display: grid;
      margin: 0;
      padding: 0 var(--space-4);
      list-style: none;
    }
    li {
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: var(--space-1) var(--space-3);
      padding: var(--space-3) 0 var(--space-3) var(--space-4);
      border-bottom: 1px solid var(--color-border);
    }
    li:last-child {
      border-bottom: 0;
    }
    .dot {
      position: absolute;
      left: 0;
      top: calc(var(--space-3) + 0.45em);
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--color-primary);
    }
    .text {
      min-width: 0;
    }
    .title {
      display: inline-block;
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    a.title {
      min-height: var(--touch-min);
      display: inline-flex;
      align-items: center;
    }
    .message {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      overflow-wrap: anywhere;
    }
    .when {
      font-size: var(--text-xs);
      white-space: nowrap;
    }
  `,
})
export class SignalsCard {
  private readonly api = inject(NotificationsService);

  protected readonly feed = resource({ loader: () => this.api.feed({ limit: FEED_LIMIT }) });
  protected readonly signals = computed(() =>
    this.feed.hasValue() ? todaysSignals(this.feed.value().items) : [],
  );
  protected readonly unread = computed(() => this.signals().filter((s) => !s.read_at));
  protected readonly marking = signal(false);

  protected link(item: FeedItemView): string | null {
    return appLink(item.deep_link);
  }

  protected ago(value: string): string {
    return formatAgo(value);
  }

  protected async markRead(): Promise<void> {
    this.marking.set(true);
    try {
      await this.api.markRead(this.unread().map((s) => s.id));
      this.feed.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.marking.set(false);
    }
  }
}
