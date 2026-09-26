import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-data-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class DataPage {
  protected readonly title = 'Data';
  protected readonly description = 'What is in the lake, how fresh it is, and ingest runs.';
  protected readonly plans = [
    'Coverage per ticker and interval: first and last bar, row count, staleness.',
    'Ingest run history with tickers ok and failed.',
    'Trigger an ingest (confirmation) and follow its progress; price chart per instrument.',
  ];
  protected readonly routes = [
    'GET /api/market/coverage',
    'GET /api/market/instruments',
    'GET /api/market/bars',
    'GET/POST /api/ingest/runs',
    'GET /api/sources',
  ];
}
