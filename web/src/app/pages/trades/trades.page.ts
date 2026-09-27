import { ChangeDetectionStrategy, Component } from '@angular/core';

import { ExportButton } from '../../shared/ui/export-button';
import { PageHeader } from '../../shared/ui/page-header';
import { TcaSummary } from './tca-summary';
import { TradeJournal } from './trade-journal';

/** Trade costs: what trading cost (implementation shortfall) and the trade journal. */
@Component({
  selector: 'app-trades-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, TcaSummary, TradeJournal, ExportButton],
  template: `
    <app-page-header
      title="Trade costs"
      description="What your orders cost against the price when they were decided, and a journal of every trade."
    >
      <app-export-button actions kind="journal" label="Journal CSV" [ghost]="true" />
    </app-page-header>
    <app-tca-summary />
    <app-trade-journal class="journal" />
  `,
  styles: `
    .journal {
      margin-top: var(--space-4);
    }
  `,
})
export class TradesPage {}
