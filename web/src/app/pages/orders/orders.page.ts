import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-orders-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class OrdersPage {
  protected readonly title = 'Orders';
  protected readonly description = 'Orders placed by ticks and the fills they received.';
  protected readonly plans = [
    'Orders table filtered by tick, strategy, ticker and status, paged from the server.',
    'Fills table with price, quantity and fee; link from an order to its fills and tick.',
  ];
  protected readonly routes = ['GET /api/orders', 'GET /api/orders/fills', 'GET /api/ticks/{id}'];
}
