import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-strategies-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class StrategiesPage {
  protected readonly title = 'Strategies';
  protected readonly description =
    'Every registered strategy, its status and its survival evidence.';
  protected readonly plans = [
    'Table of strategies with status (active, shadow, retired), class and asset classes, filterable by status.',
    'Strategy detail: parameters, survival reports with pass/fail per test and their metrics.',
    'Promote, move to shadow and retire, each behind a confirmation; promote needs the strategy id typed.',
  ];
  protected readonly routes = [
    'GET /api/strategies',
    'GET /api/strategies/{id}',
    'POST /api/strategies/{id}/promote',
    'POST /api/strategies/{id}/shadow',
    'POST /api/strategies/{id}/retire',
  ];
}
