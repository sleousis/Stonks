import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-shadow-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class ShadowPage {
  protected readonly title = 'Shadow';
  protected readonly description = 'Shadow strategies trading on paper next to the active ones.';
  protected readonly plans = [
    'P&L summary per shadow strategy against the active portfolio.',
    'Per-strategy shadow equity curve and decisions log.',
  ];
  protected readonly routes = [
    'GET /api/shadow/pnl',
    'GET /api/shadow/strategies/{id}/pnl',
    'GET /api/shadow/decisions',
  ];
}
