import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-lab-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class LabPage {
  protected readonly title = 'Lab';
  protected readonly description = 'Backtest strategies and run the tune, fit and survival suite.';
  protected readonly plans = [
    'Backtest form: strategy class, parameters, tickers, dates, interval, cost model.',
    'Lab run form: tuner, objective, trials, survival tests; live progress over the job stream; cancel.',
    'Results: equity curve with drawdown, metrics, survival verdicts, recent jobs.',
  ];
  protected readonly routes = [
    'POST /api/lab/backtests',
    'GET /api/lab/backtests/{job}/result',
    'POST /api/lab/runs',
    'GET /api/lab/runs/{job}/result',
    'GET /api/lab/cost-models',
    'GET /api/catalog/strategies',
    'GET /api/jobs',
  ];
}
