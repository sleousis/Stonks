import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';

import { FactorsService } from '../../../api/factors.service';
import type { FactorTearSheetView, UniverseView, WatchlistView } from '../../../api/models';
import { SessionService } from '../../../core/auth/session.service';
import { JobsService } from '../../../core/jobs/jobs.service';
import { ToastService } from '../../../core/notify/toast.service';
import { JobProgress } from '../../../shared/ui/job-progress';
import { PermissionNote } from '../../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../../shared/ui/states';
import { JobFollower } from '../job-follower';
import { BasketPicker } from './basket-picker';
import {
  type TearsheetForm,
  basketText,
  buildTearsheetRequest,
  defaultTearsheetForm,
  tearsheetErrors,
} from './factor-requests';
import { FactorTearsheetResult } from './tearsheet-result';

/**
 * Runs a factor tear sheet as a background job, follows it over SSE and
 * shows the result.
 */
@Component({
  selector: 'app-factor-tearsheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BasketPicker,
    FactorTearsheetResult,
    JobProgress,
    PermissionNote,
    ErrorState,
    LoadingState,
  ],
  template: `
    @let f = form();
    <form class="form" novalidate (submit)="$event.preventDefault(); start()">
      <app-basket-picker
        idPrefix="ts"
        [basket]="f.basket"
        [universes]="universes()"
        [watchlists]="watchlists()"
        [error]="errors()['basket']"
        (basketChange)="patch({ basket: $event })"
      />
      <div class="grid">
        <div class="field">
          <label for="ts-start">From</label>
          <input
            id="ts-start"
            class="input"
            type="date"
            [value]="f.start"
            [attr.aria-invalid]="!!errors()['start']"
            (input)="patch({ start: $any($event.target).value })"
          />
          @if (errors()['start']; as e) {
            <span class="error">{{ e }}</span>
          }
        </div>
        <div class="field">
          <label for="ts-end">To</label>
          <input
            id="ts-end"
            class="input"
            type="date"
            [value]="f.end"
            [attr.aria-invalid]="!!errors()['end']"
            [attr.aria-describedby]="errors()['end'] ? 'ts-end-error' : null"
            (input)="patch({ end: $any($event.target).value })"
          />
          @if (errors()['end']; as e) {
            <span class="error" id="ts-end-error">{{ e }}</span>
          }
        </div>
        <div class="field">
          <label for="ts-horizons">Look ahead (bars)</label>
          <input
            id="ts-horizons"
            class="input num"
            inputmode="numeric"
            autocomplete="off"
            aria-describedby="ts-horizons-hint"
            [value]="f.horizons"
            [attr.aria-invalid]="!!errors()['horizons']"
            (input)="patch({ horizons: $any($event.target).value })"
          />
          @if (errors()['horizons']; as e) {
            <span class="error" id="ts-horizons-hint">{{ e }}</span>
          } @else {
            <span class="hint" id="ts-horizons-hint">One day, a week and a month: 1, 5, 21.</span>
          }
        </div>
        <div class="field">
          <label for="ts-quantiles">Buckets</label>
          <input
            id="ts-quantiles"
            class="input num"
            type="number"
            inputmode="numeric"
            min="2"
            max="20"
            step="1"
            aria-describedby="ts-quantiles-hint"
            [value]="f.quantiles ?? ''"
            [attr.aria-invalid]="!!errors()['quantiles']"
            (input)="setQuantiles($any($event.target).value)"
          />
          @if (errors()['quantiles']; as e) {
            <span class="error" id="ts-quantiles-hint">{{ e }}</span>
          } @else {
            <span class="hint" id="ts-quantiles-hint"
              >Names split into this many groups by value.</span
            >
          }
        </div>
      </div>
      <div class="actions">
        <button
          class="btn btn-primary"
          type="submit"
          [disabled]="starting() || !canRun() || !factor()"
        >
          {{ starting() ? 'Starting' : 'Run tear sheet' }}
        </button>
        @if (!canRun()) {
          <app-permission-note permission="lab.run" />
        }
      </div>
    </form>

    @if (run.handle(); as h) {
      @if (!h.done() || h.status() !== 'succeeded') {
        <app-job-progress label="Tear sheet" [handle]="h" />
      }
      @if (run.error(); as err) {
        <app-error-state
          title="Could not load the tear sheet"
          [error]="err"
          (retry)="run.retry()"
        />
      } @else if (run.loading()) {
        <app-loading-state label="Loading the tear sheet" [rows]="6" />
      } @else if (run.result(); as r) {
        <app-factor-tearsheet-result [result]="r" />
      }
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .form {
      display: grid;
      gap: var(--space-3);
    }
    .grid {
      display: grid;
      gap: var(--space-3);
      @include bp.from-tablet {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-3);
    }
  `,
})
export class FactorTearsheet {
  private readonly factors = inject(FactorsService);
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);

  /** A library id or a checked formula. */
  readonly factor = input.required<string>();
  readonly universes = input<readonly UniverseView[]>([]);
  readonly watchlists = input<readonly WatchlistView[]>([]);

  protected readonly canRun = computed(() => this.session.can('lab.run'));
  protected readonly form = signal<TearsheetForm>(defaultTearsheetForm());
  protected readonly tried = signal(false);
  protected readonly starting = signal(false);
  private readonly allErrors = computed(() => tearsheetErrors(this.form(), this.watchlists()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));

  protected readonly run = new JobFollower<FactorTearSheetView>(
    inject(JobsService),
    inject(DestroyRef),
    (id) => this.factors.tearsheetResult(id),
  );

  protected patch(p: Partial<TearsheetForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setQuantiles(raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ quantiles: n === null || Number.isNaN(n) ? null : n });
  }

  async start(): Promise<void> {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    const f = this.form();
    const body = buildTearsheetRequest(this.factor(), f, this.watchlists());
    this.starting.set(true);
    let jobId: string;
    try {
      jobId = (await this.factors.startTearsheet(body)).id;
    } catch {
      return; // toasted by the error interceptor
    } finally {
      this.starting.set(false);
    }
    this.toasts.success(`Started the tear sheet on ${basketText(f.basket, this.watchlists())}.`);
    await this.run.follow(jobId);
  }
}
