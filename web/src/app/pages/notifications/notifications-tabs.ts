import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/**
 * The views under Notifications: the feed, the price alerts that feed it,
 * and every alert setting in one place (what reaches you, where and when).
 */
export const NOTIFICATIONS_TABS = [
  { path: '/notifications', label: 'Feed' },
  { path: '/notifications/price-alerts', label: 'Price alerts' },
  { path: '/notifications/settings', label: 'Alert settings' },
] as const;

/**
 * The tab bar of the Feed, Price alerts and Alert settings, one tap apart. Plain links,
 * the current one marked with aria-current. 44px tall on phones.
 */
@Component({
  selector: 'app-notifications-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="tabs" aria-label="Notifications views">
      @for (t of tabs; track t.path) {
        <a
          [routerLink]="t.path"
          routerLinkActive="active"
          ariaCurrentWhenActive="page"
          [routerLinkActiveOptions]="{ exact: true }"
          >{{ t.label }}</a
        >
      }
    </nav>
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: block;
      min-width: 0;
    }
    .tabs {
      display: flex;
      gap: var(--space-1);
      margin: calc(-1 * var(--space-2)) 0 var(--space-4);
      border-bottom: 1px solid var(--color-border);
      overflow-x: auto;
      scrollbar-width: none;
    }
    .tabs a {
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 0 var(--space-3);
      margin-bottom: -1px;
      border-bottom: 2px solid transparent;
      color: var(--color-ink-2);
      font-size: var(--text-sm);
      font-weight: var(--weight-medium);
      text-decoration: none;
      white-space: nowrap;
    }
    .tabs a:hover {
      color: var(--color-ink);
    }
    .tabs a.active {
      color: var(--color-ink);
      border-bottom-color: var(--color-accent);
    }
    @include bp.phone {
      .tabs a {
        flex: 1 1 auto;
        justify-content: center;
        min-height: var(--touch-min);
      }
    }
    @include bp.coarse {
      .tabs a {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class NotificationsTabs {
  protected readonly tabs = NOTIFICATIONS_TABS;
}
