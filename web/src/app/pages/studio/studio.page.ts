import { ChangeDetectionStrategy, Component } from '@angular/core';

import { PlannedPage } from '../../shared/ui/planned-page';

/** Placeholder: replace the body with the real page (see docs/ui.md, "Add a page"). */
@Component({
  selector: 'app-studio-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PlannedPage],
  template: `<app-planned-page
    [title]="title"
    [description]="description"
    [plans]="plans"
    [routes]="routes"
  />`,
})
export class StudioPage {
  protected readonly title = 'Studio';
  protected readonly description = 'Define a rule-based strategy, test it, and send it to shadow.';
  protected readonly plans = [
    'Drafts list; create from a template or blank.',
    'Visual rule builder (indicators, entry and exit conditions, sizing, asset classes) with a live JSON view and validation.',
    'One-click backtest with equity curve, trades and metrics; lab run with survival verdicts.',
    'Register a draft into shadow, then enable or disable it with a toggle.',
  ];
  protected readonly routes = [
    'GET/POST /api/studio/drafts',
    'GET/PATCH/DELETE /api/studio/drafts/{id}',
    'POST /api/studio/drafts/{id}/validate',
    'POST /api/studio/drafts/{id}/backtests',
    'POST /api/studio/drafts/{id}/lab-runs',
    'POST /api/studio/drafts/{id}/register',
    'POST /api/studio/drafts/{id}/enable|disable',
    'GET /api/studio/schema',
    'GET /api/studio/templates',
  ];
}
