import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';

/** The views under Orders. Trade costs lives at its own address but sits here too. */
export const ORDERS_TABS = [
  { path: '/orders', label: 'Orders', exact: true },
  { path: '/orders/fills', label: 'Fills', exact: false },
  { path: '/orders/ticks', label: 'Trading runs', exact: false },
  { path: '/trades', label: 'Trade costs', exact: true },
] as const;

/**
 * The tab bar shared by the Orders views and Trade costs, so each is one tap
 * from the other. Plain links (each view has its own address), the current
 * one marked with aria-current. 44px tall on phones and touch screens.
 */
@Component({
  selector: 'app-orders-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="tabs" aria-label="Orders views">
      @for (t of tabs; track t.path) {
        <a
          [routerLink]="t.path"
          routerLinkActive="active"
          ariaCurrentWhenActive="page"
          [routerLinkActiveOptions]="{ exact: t.exact }"
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
      transition: color var(--dur-fast) var(--ease);
    }
    .tabs a:hover {
      color: var(--color-ink);
    }
    .tabs a.active {
      color: var(--color-ink);
      border-bottom-color: var(--color-accent);
    }
    @include bp.phone {
      .tabs {
        gap: 0;
      }
      .tabs a {
        flex: 1 1 auto;
        justify-content: center;
        min-height: var(--touch-min);
        padding: 0 var(--space-2);
      }
    }
    @include bp.coarse {
      .tabs a {
        min-height: var(--touch-min);
      }
    }
  `,
})
export class OrdersTabs {
  protected readonly tabs = ORDERS_TABS;
}
