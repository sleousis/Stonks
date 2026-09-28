import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PageHeader } from '../../shared/ui/page-header';
import { NotificationFeed } from './notification-feed';
import { NotificationsTabs } from './notifications-tabs';
import { PushDevices } from './push-devices';

/** The in-app feed and the devices that get push. */
@Component({
  selector: 'app-notifications-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, NotificationsTabs, NotificationFeed, PushDevices],
  template: `
    <app-page-header
      title="Notifications"
      description="Everything the console told you, newest first. It all lands here, even when push is off."
    />
    <app-notifications-tabs />
    <div class="stack">
      <app-notification-feed />
      <app-push-devices />
    </div>
  `,
  styles: `
    .stack {
      display: grid;
      gap: var(--space-5);
      min-width: 0;
      max-width: 60rem;
    }
  `,
})
export class NotificationsPage {}
