import { ChangeDetectionStrategy, Component } from '@angular/core';

import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';

/**
 * The views under Notifications: the feed, the price alerts that feed it,
 * and every alert setting in one place (what reaches you, where and when).
 */
export const NOTIFICATIONS_TABS = [
  { path: '/notifications', label: 'Feed', exact: true },
  { path: '/notifications/price-alerts', label: 'Price alerts' },
  { path: '/notifications/settings', label: 'Alert settings' },
] as const;

/**
 * The tab bar of the Feed, Price alerts and Alert settings, one tap apart:
 * the console's one tab style (`<app-page-tabs>`, M4), plain links with the
 * current one marked by aria-current.
 */
@Component({
  selector: 'app-notifications-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageTabs],
  template: `<app-page-tabs label="Notifications views" [tabs]="tabs" />`,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
  `,
})
export class NotificationsTabs {
  protected readonly tabs: readonly PageTab[] = NOTIFICATIONS_TABS;
}
