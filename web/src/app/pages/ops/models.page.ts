import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
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
 * runs as a test book and waits for a swap or a rejection, and a retrain
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
    <app-page-header title="Model versions" [description]="standfirst()" />

    <div class="cols-facts">
      <section class="page-section" aria-labelledby="candidates-title">
        <div class="section-head">
          <h2 id="candidates-title">
            Candidates
            @if (candidates.hasValue()) {
              <span class="count">{{ candidates.value().length }}</span>
            }
          </h2>
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
            message="A retrain runs every Saturday and each new version waits here until it is put in use or rejected. Only strategies that learn from data get new versions, so this stays empty while none of them is approved or on trial. Use Retrain all to retrain now."
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

      <section class="page-section" aria-labelledby="retrain-title">
        <div class="section-head">
          <h2 id="retrain-title">Retrain</h2>
        </div>
        <div class="retrain">
          <p class="callout">
            Retrains every strategy that learns from data on its latest data. The weekly run does
            the same. A strategy retrained in the last few days is skipped unless you check the box.
          </p>
          <app-retrain-job label="Retrain all" (finished)="candidates.reload()" />
        </div>
      </section>
    </div>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .retrain {
      display: grid;
      gap: var(--space-4);
      padding-top: var(--space-4);
    }
    .callout {
      font-size: var(--text-sm);
    }
  `,
})
export class ModelsPage {
  /** One sentence from the list: how many versions wait for a decision. */
  protected readonly standfirst = computed(() => {
    const words = 'Each trades on its own test book until an admin decides.';
    if (!this.candidates.hasValue())
      return `New versions of strategies that learn from data. ${words}`;
    const n = this.candidates.value().length;
    if (n === 0) return `No new version waits for a decision. A retrain runs every Saturday.`;
    return `${n === 1 ? '1 new version waits' : n + ' new versions wait'} for a decision. ${words}`;
  });

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
