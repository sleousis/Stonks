import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-health-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class HealthPage {
  protected readonly title = 'Health';
  protected readonly description = 'Whether the system is fresh, consistent and ticking.';
  protected readonly plans = [
    'Every health check with its detail, grouped by pass and fail.',
    'Freshness for chosen tickers; recent tick failures and job failures.',
  ];
  protected readonly routes = [
    'GET /api/health',
    'GET /api/health/report',
    'GET /api/ticks?status=failed',
    'GET /api/jobs?status=failed',
  ];
}
