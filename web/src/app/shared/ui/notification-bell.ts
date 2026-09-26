import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
} from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

import { NotificationFeedService } from '../../core/notify/notification-feed.service';

/**
 * A bell linking to the notifications page, with the unread count. Starts
 * the quiet unread-count poll on its own, so the shell only places it:
 *
 *   <app-notification-bell />
 */
@Component({
  selector: 'app-notification-bell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <a
      class="btn btn-ghost bell"
      routerLink="/notifications"
      routerLinkActive="current"
      ariaCurrentWhenActive="page"
      [attr.aria-label]="label()"
      [attr.title]="label()"
    >
      <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
        <path
          d="M10 3a4.5 4.5 0 0 0-4.5 4.5v3.2L4 13.5h12l-1.5-2.8V7.5A4.5 4.5 0 0 0 10 3Z"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linejoin="round"
        />
        <path
          d="M8.2 15.8a1.9 1.9 0 0 0 3.6 0"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
        />
      </svg>
      @if (count() > 0) {
        <span class="badge num" aria-hidden="true">{{ badge() }}</span>
      }
    </a>
  `,
  styles: `
    :host {
      display: inline-flex;
    }
    .bell {
      position: relative;
      width: var(--touch-min);
      height: var(--touch-min);
      min-height: var(--touch-min);
      padding: 0;
    }
    .bell.current {
      color: var(--color-primary);
    }
    .badge {
      position: absolute;
      top: 4px;
      right: 2px;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border: 2px solid var(--color-surface);
      border-radius: 999px;
      background: var(--color-loss);
      color: var(--color-surface);
      font-size: 10px;
      font-weight: var(--weight-semibold);
      line-height: 14px;
      text-align: center;
      box-sizing: border-box;
    }
  `,
})
export class NotificationBell implements OnInit {
  private readonly feed = inject(NotificationFeedService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly count = this.feed.unread;
  protected readonly badge = computed(() => (this.count() > 99 ? '99+' : String(this.count())));
  protected readonly label = computed(() =>
    this.count() > 0 ? `Notifications, ${this.count()} unread` : 'Notifications',
  );

  ngOnInit(): void {
    this.feed.watch(this.destroyRef);
  }
}
