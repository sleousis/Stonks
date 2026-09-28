import { ChangeDetectionStrategy, Component } from '@angular/core';

import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';

/** The insights screens, as links so each has its own address. */
export const INSIGHTS_SECTIONS = [
  { path: '/insights', label: 'Overview', exact: true },
  { path: '/insights/risk', label: 'Risk', exact: false },
  { path: '/insights/cash-flows', label: 'Cash flows', exact: false },
  { path: '/insights/tax', label: 'Tax', exact: false },
  { path: '/insights/behaviour', label: 'Behaviour', exact: false },
] as const;

/** The Insights screens in the console's one tab style (`<app-page-tabs>`, M4). */
@Component({
  selector: 'app-insights-nav',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageTabs],
  template: `<app-page-tabs label="Insights screens" [tabs]="sections" />`,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
  `,
})
export class InsightsNav {
  protected readonly sections: readonly PageTab[] = INSIGHTS_SECTIONS;
}
