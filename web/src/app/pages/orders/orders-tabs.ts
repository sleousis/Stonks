import { ChangeDetectionStrategy, Component } from '@angular/core';

import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';

/** The views under Orders. Trade costs lives at its own address but sits here too. */
export const ORDERS_TABS = [
  { path: '/orders', label: 'Orders', exact: true },
  { path: '/orders/new', label: 'New order', exact: true },
  { path: '/orders/rebalance', label: 'Rebalance', exact: true },
  { path: '/orders/fills', label: 'Fills', exact: false },
  { path: '/orders/ticks', label: 'Trading runs', exact: false },
  { path: '/trades', label: 'Trade costs', exact: true },
  { path: '/journal', label: 'Journal', exact: false },
] as const;

/**
 * The tab bar shared by the Orders views and Trade costs, so each is one tap
 * from the other: the console's one tab style (`<app-page-tabs>`, M4), plain
 * links with the current one marked by aria-current.
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
