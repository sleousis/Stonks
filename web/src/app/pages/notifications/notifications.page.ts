import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { NotificationFeedService } from '../../core/notify/notification-feed.service';
import { PageHeader } from '../../shared/ui/page-header';
import { NotificationFeed } from './notification-feed';
import { NotificationsTabs } from './notifications-tabs';

/** "3 unread. Everything lands here, even when push is off." */
export function inboxStandfirst(unread: number | null): string {
  const lead =
    unread == null
      ? 'Everything the console told you, newest first.'
      : unread === 0
        ? 'You are all caught up.'
        : `${unread} unread, newest first.`;
  return `${lead} It all lands here, even when push is off.`;
}

/** The inbox. Where alerts reach you lives on the Settings tab. */
@Component({
  selector: 'app-notifications-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, NotificationsTabs, NotificationFeed],
  template: `
    <app-page-header title="Notifications" [description]="standfirst()" />
    <app-notifications-tabs />
    <app-notification-feed />
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    app-notifications-tabs {
      margin-bottom: var(--space-6);
    }
  `,
})
export class NotificationsPage {
  private readonly counter = inject(NotificationFeedService);
  protected readonly standfirst = computed(() => inboxStandfirst(this.counter.unread()));
}
