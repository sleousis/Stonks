import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-go-live-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class GoLivePage {
  protected readonly title = 'Go-live';
  protected readonly description =
    'Checks a strategy must pass before it trades with a real broker.';
  protected readonly plans = [
    'Go-live gate per strategy: paper days, drawdown, drift against the backtest, trade count, each pass or fail.',
    'Broker connection and account status; risk policy limits.',
    'Run a tick (typed confirmation), dry run first by default, and follow it live.',
  ];
  protected readonly routes = [
    'GET /api/brokers',
    'GET /api/brokers/alpaca/status',
    'GET /api/risk/policy',
    'POST /api/ticks',
  ];
}
