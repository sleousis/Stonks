import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PageHeader } from '../../shared/ui/page-header';
import { NotificationFeed } from './notification-feed';
import { NotificationsTabs } from './notifications-tabs';

/** The in-app feed. Where alerts reach you lives on the Alert settings tab. */
@Component({
  selector: 'app-notifications-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, NotificationsTabs, NotificationFeed],
  template: `
    <app-page-header
      title="Notifications"
      description="Everything the console told you, newest first. It all lands here, even when push is off."
    />
    <app-notifications-tabs />
    <app-notification-feed />
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
  `,
})
export class NotificationsPage {}
