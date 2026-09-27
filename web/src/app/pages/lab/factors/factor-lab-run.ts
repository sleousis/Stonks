import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';

import { LabService } from '../../../api/lab.service';
import type { FactorView, LabRunView, UniverseView, WatchlistView } from '../../../api/models';
import { SystemService } from '../../../api/system.service';
import { SessionService } from '../../../core/auth/session.service';
import { ConfirmService } from '../../../core/confirm/confirm.service';
import { JobsService } from '../../../core/jobs/jobs.service';
import { ToastService } from '../../../core/notify/toast.service';
import { LabRunResultView } from '../../../shared/lab-results/lab-run-result';
import { JobProgress } from '../../../shared/ui/job-progress';
import { PermissionNote } from '../../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../../shared/ui/states';
import { JobFollower } from '../job-follower';
import { BasketPicker } from './basket-picker';
import {
  FACTOR_STRATEGY_CLASS,
  type FactorRunForm,
  type FactorSuite,
  basketText,
  buildFactorRunRequest,
  defaultFactorRunForm,
  factorRunErrors,
} from './factor-requests';

const SUITES: readonly { id: FactorSuite; label: string }[] = [
  { id: 'quick', label: 'Quick: held-out data and each sub-period' },
  { id: 'standard', label: 'Standard: adds noise, walk-forward and costs' },
  { id: 'promotion', label: 'Go-live suite: every check, slow' },
];

/**
 * Tests a factor as a strategy: the `factor` strategy holds the top slice
 * of the names by the factor, rebalanced on month ends. The factor stays
 * fixed while the lab tunes the slice and runs a survival suite. Nothing
 * starts paper trading from here.
 */
@Component({
  selector: 'app-factor-lab-run',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BasketPicker, JobProgress, LabRunResultView, PermissionNote, ErrorState, LoadingState],
  template: `
    @let f = form();
    <form class="form" novalidate (submit)="$event.preventDefault(); start()">
      <app-basket-picker
        idPrefix="fr"
        [basket]="f.basket"
        [universes]="universes()"
        [watchlists]="watchlists()"
        [error]="errors()['basket']"
        (basketChange)="patch({ basket: $event })"
      />
      <div class="grid">
        <div class="field">
          <label for="fr-start">From</label>
          <input
            id="fr-start"
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
          <label for="fr-end">To</label>
          <input
            id="fr-end"
            class="input"
            type="date"
            [value]="f.end"
            [attr.aria-invalid]="!!errors()['end']"
            (input)="patch({ end: $any($event.target).value })"
          />
          @if (errors()['end']; as e) {
            <span class="error">{{ e }}</span>
          }
        </div>
        <div class="field">
          <label for="fr-suite">Checks</label>
          <select
            id="fr-suite"
            class="input"
            (change)="patch({ suite: $any($event.target).value })"
          >
            @for (s of suites; track s.id) {
              <option [value]="s.id" [selected]="f.suite === s.id">{{ s.label }}</option>
            }
          </select>
        </div>
        <div class="field">
          <label for="fr-budget">Trials</label>
          <input
            id="fr-budget"
            class="input num"
            type="number"
            inputmode="numeric"
            min="1"
            max="1000"
            step="1"
            aria-describedby="fr-budget-hint"
            [value]="f.budget ?? ''"
            [attr.aria-invalid]="!!errors()['budget']"
            (input)="setBudget($any($event.target).value)"
          />
          @if (errors()['budget']; as e) {
            <span class="error" id="fr-budget-hint">{{ e }}</span>
          } @else {
            <span class="hint" id="fr-budget-hint">
              Slices of names to try. Every one counts against the factor strategy.
            </span>
          }
        </div>
      </div>
      <div class="field">
        <label for="fr-hypothesis">Why it should work</label>
        <textarea
          id="fr-hypothesis"
          class="input"
          rows="3"
          aria-describedby="fr-hypothesis-hint"
          [value]="f.hypothesis"
          [attr.aria-invalid]="!!errors()['hypothesis']"
          (input)="patch({ hypothesis: $any($event.target).value })"
        ></textarea>
        @if (errors()['hypothesis']; as e) {
          <span class="error" id="fr-hypothesis-hint">{{ e }}</span>
        } @else {
          <span class="hint" id="fr-hypothesis-hint">{{ hypothesisHint() }}</span>
        }
      </div>
      <div class="actions">
        <button
          class="btn btn-primary"
          type="submit"
          [disabled]="starting() || !canRun() || !factor()"
        >
          {{ starting() ? 'Starting' : 'Start lab run' }}
        </button>
        @if (!canRun()) {
          <app-permission-note permission="lab.run" />
        }
      </div>
    </form>

    @if (run.handle(); as h) {
      @if (!h.done() || h.status() !== 'succeeded') {
        <app-job-progress label="Lab run" [handle]="h" />
      }
      @if (run.error(); as err) {
        <app-error-state
          title="Could not load the lab run result"
          [error]="err"
          (retry)="run.retry()"
        />
      } @else if (run.loading()) {
        <app-loading-state label="Loading the lab run result" [rows]="6" />
      } @else if (run.result(); as r) {
        <app-lab-run-result [result]="r" />
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
export class FactorLabRun {
  private readonly lab = inject(LabService);
  private readonly system = inject(SystemService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  /** A library id or a checked formula. */
  readonly factor = input.required<string>();
  /** The library factor, when it is one: its hypothesis starts the form. */
  readonly info = input<FactorView | null>(null);
  readonly universes = input<readonly UniverseView[]>([]);
  readonly watchlists = input<readonly WatchlistView[]>([]);

  protected readonly suites = SUITES;
  protected readonly canRun = computed(() => this.session.can('lab.run'));
  protected readonly form = linkedSignal<FactorView | null, FactorRunForm>({
    source: () => this.info(),
    computation: (info) => defaultFactorRunForm(info),
  });
  protected readonly tried = signal(false);
  protected readonly starting = signal(false);
  private readonly allErrors = computed(() => factorRunErrors(this.form(), this.watchlists()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly hypothesisHint = computed(() =>
    this.info()
      ? 'The factor’s own reason, recorded with the run. Edit it to say what you expect.'
      : 'A formula has no reason of its own. Write one before you test it.',
  );

  /** The `factor` strategy's class, from the catalog when it loads. */
  private readonly classes = resource({ loader: () => this.system.strategyClasses() });
  private readonly classPath = computed(() => {
    const list = this.classes.hasValue() ? this.classes.value() : [];
    return list.find((c) => c.name === 'factor')?.class_path ?? FACTOR_STRATEGY_CLASS;
  });

  protected readonly run = new JobFollower<LabRunView>(
    inject(JobsService),
    inject(DestroyRef),
    (id) => this.lab.labRunResult(id),
  );

  protected patch(p: Partial<FactorRunForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setBudget(raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ budget: n === null || Number.isNaN(n) ? null : n });
  }

  async start(): Promise<void> {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    const f = this.form();
    const where = basketText(f.basket, this.watchlists());
    const ok = await this.confirm.confirm({
      title: `Test ${this.shortName()} as a strategy?`,
      message: `It holds the top slice of ${where} by this factor, rebalanced each month end, ${f.start} to ${f.end}. Every trial is counted in the trial ledger. It runs in the background.`,
      confirmLabel: 'Start lab run',
    });
    if (!ok) return;
    const body = buildFactorRunRequest(this.factor(), this.classPath(), f, this.watchlists());
    this.starting.set(true);
    let jobId: string;
    try {
      jobId = (await this.lab.startLabRun(body)).id;
    } catch {
      return; // toasted by the error interceptor
    } finally {
      this.starting.set(false);
    }
    this.toasts.success(`Started the lab run of ${this.shortName()}.`);
    await this.run.follow(jobId);
  }

  private shortName(): string {
    const f = this.factor();
    return f.length > 40 ? 'this formula' : f;
  }
}
