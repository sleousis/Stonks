import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { type ModelVersionView, ModelVersionsService } from '../../api/model-versions.service';
import { trainWindow, versionLook } from '../../shared/model-versions';
import { strategyDisplayName } from '../../shared/strategy-names';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { RetrainJob } from '../../shared/ui/retrain-job';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

/**
 * Model versions across strategies (roadmap 22.6): every candidate that
 * runs as a model book and waits for a swap or a rejection, and a retrain
 * of every strategy that learns from data. Swaps happen on each strategy's
 * Model versions tab, next to its swap check.
 */
@Component({
  selector: 'app-models-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    DataTable,
    TableCell,
    RetrainJob,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Model versions"
      description="New fits of strategies that learn from data. Each trades on its own test book until an admin swaps it in or rejects it."
    />

    <section class="panel" aria-labelledby="candidates-title">
      <div class="panel-head">
        <h2 id="candidates-title">Candidates</h2>
        @if (candidates.hasValue()) {
          <span class="muted num">{{ candidates.value().length }}</span>
        }
      </div>
      @if (candidates.error(); as err) {
        <app-error-state
          title="Could not load the candidates"
          [error]="err"
          (retry)="candidates.reload()"
        />
      } @else if (!candidates.hasValue()) {
        <app-loading-state label="Loading candidates" [rows]="3" />
      } @else if (candidates.value().length === 0) {
        <app-empty-state
          title="No candidates"
          message="A retrain runs every Saturday and each new fit waits here until it is swapped in or rejected. Only strategies that learn from data get new fits, so this stays empty while none of them is approved or on trial. Use Retrain all below to refit now."
        />
      } @else {
        <app-data-table
          caption="Candidate models, one per strategy"
          [rows]="candidates.value()"
          [columns]="columns"
          [rowKey]="key"
        >
          <ng-template appCell="strategy" [appCellOf]="candidates.value()" let-v>
            <a
              [routerLink]="['/strategies', v.strategy_id]"
              [queryParams]="{ tab: 'versions' }"
              [attr.aria-label]="'Review v' + v.version + ' of ' + name(v.strategy_id)"
              >{{ name(v.strategy_id) }}</a
            >
          </ng-template>
          <ng-template appCell="version" [appCellOf]="candidates.value()" let-v>
            <span class="num">v{{ v.version }}</span>
          </ng-template>
        </app-data-table>
      }
    </section>

    <section class="panel" aria-labelledby="retrain-title">
      <div class="panel-head">
        <h2 id="retrain-title">Retrain</h2>
      </div>
      <div class="panel-body">
        <p class="muted">
          Refits every strategy that learns from data on its latest data. The weekly run does the
          same. A strategy fitted in the last few days is skipped unless you refit anyway.
        </p>
        <app-retrain-job label="Retrain all" (finished)="candidates.reload()" />
      </div>
    </section>
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .muted {
      color: var(--color-ink-2);
    }
    .panel-body {
      display: grid;
      gap: var(--space-3);
    }
  `,
})
export class ModelsPage {
  private readonly api = inject(ModelVersionsService);

  protected readonly candidates = resource({ loader: () => this.api.candidates() });

  protected readonly columns: readonly TableColumn<ModelVersionView>[] = [
    {
      key: 'strategy',
      label: 'Strategy',
      mobile: 'title',
      value: (v) => strategyDisplayName(v.strategy_id),
    },
    { key: 'version', label: 'Version', value: (v) => v.version },
    { key: 'status', label: 'Status', value: (v) => versionLook(v.status).label },
    { key: 'train', label: 'Trained on', value: (v) => trainWindow(v), sortable: false },
    { key: 'created_at', label: 'Fitted', format: 'date' },
  ];
  protected readonly key = (v: ModelVersionView) => `${v.strategy_id}@${v.version}`;
  protected readonly name = (id: string) => strategyDisplayName(id);
}
