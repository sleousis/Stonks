import { ChangeDetectionStrategy, Component } from '@angular/core';

import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';

/**
 * The views under Orders: three tabs that fit a phone (PO-11). Rebalance is
 * a header button, trading runs a link above the list, and Trade costs and
 * Journal live in the nav.
 */
export const ORDERS_TABS = [
  { path: '/orders', label: 'Orders', exact: true },
  { path: '/orders/new', label: 'New order', exact: true },
  { path: '/orders/fills', label: 'Fills', exact: false },
] as const;

/**
 * The tab bar of the Orders views: the console's one tab style
 * (`<app-page-tabs>`, M4), plain links with the current one marked by
 * aria-current.
 */
@Component({
  selector: 'app-orders-tabs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageTabs],
  template: `<app-page-tabs label="Orders views" [tabs]="tabs" />`,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
  `,
})
export class OrdersTabs {
  protected readonly tabs: readonly PageTab[] = ORDERS_TABS;
}
