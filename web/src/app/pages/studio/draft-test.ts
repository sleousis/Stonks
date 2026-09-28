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

import { JobsApiService } from '../../api/jobs-api.service';
import { LabService } from '../../api/lab.service';
import { MarketService } from '../../api/market.service';
import type {
  BacktestResult,
  Draft,
  DraftBacktestRequest,
  DraftLabRunRequest,
  LabRunView,
} from '../../api/models';
import { StudioService } from '../../api/studio.service';
import { SessionService } from '../../core/auth/session.service';
import { formatPercent } from '../../core/format/format';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { BacktestResultView } from '../../shared/lab-results/backtest-result';
import { LabRunResultView } from '../../shared/lab-results/lab-run-result';
import { HelpTip } from '../../shared/ui/help-tip';
import { PermissionNote } from '../../shared/ui/permission-note';
import { StatusPill } from '../../shared/ui/status-pill';
import { CostField } from '../lab/cost-field';
import {
  type CostForm,
  PICKABLE_TESTS,
  SUITES,
  SURVIVAL_TESTS,
  type SuiteChoice,
  type SurvivalTestName,
  costErrors,
  costFields,
  defaultWindow,
  parseTickers,
  suiteTests,
  suitesFromPresets,
} from '../lab/lab-requests';
import { INTERVALS } from './rule-spec';

/**
 * Test a draft: a backtest (equity, drawdown, metrics) and a lab run
 * (survival verdicts), each a background job followed live. Unsaved builder
 * changes are saved first through `ensureSaved`.
 */
@Component({
  selector: 'app-draft-test',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BacktestResultView, LabRunResultView, StatusPill, PermissionNote, CostField, HelpTip],
  templateUrl: './draft-test.html',
  styleUrl: './draft-test.scss',
})
export class DraftTest {
  private readonly studio = inject(StudioService);
  private readonly lab = inject(LabService);
  private readonly market = inject(MarketService);
  private readonly jobs = inject(JobsService);
  private readonly jobsApi = inject(JobsApiService);
  private readonly toasts = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly session = inject(SessionService);

  /** Backtests and lab runs need `lab.run`. */
  protected readonly canLab = computed(() => this.session.can('lab.run'));

  readonly draft = input.required<Draft>();
  /** Interval and allow-list of the spec being edited (defaults for the form). */
  readonly specInterval = input('1d');
  readonly specTickers = input<readonly string[] | null>(null);
  readonly assetClass = input('equity');
  /** Saves pending edits; resolves false when the draft could not be saved. */
  readonly ensureSaved = input<() => Promise<boolean>>(() => Promise.resolve(true));

  protected readonly intervals = INTERVALS;
  protected readonly pickable = PICKABLE_TESTS;

  // ---- setup form ------------------------------------------------------------
  protected readonly tickersText = linkedSignal(() => (this.specTickers() ?? []).join(', '));
  protected readonly start = signal(defaultWindow().start);
  protected readonly end = signal(defaultWindow().end);
  protected readonly interval = linkedSignal(() => this.specInterval());
  protected readonly initialCash = signal(10_000);
  protected readonly rebalanceEvery = signal(1);
  /** Realistic by default: the costs the admin configured (P18), as in the Lab. */
  protected readonly costForm = signal<CostForm>({
    cost: 'configured',
    slippageBps: 5,
    feePerTrade: 0,
  });
  protected readonly suite = signal<SuiteChoice>('quick');
  /** The tests ticked for a custom suite. */
  protected readonly tests = signal<SurvivalTestName[]>(['oos', 'period_stability']);
  protected readonly objective = signal<'sharpe' | 'cagr' | 'final_return'>('sharpe');
  protected readonly submitted = signal(false);

  protected readonly costModels = resource({ loader: () => this.lab.costModels() });
  protected readonly presets = computed(() =>
    this.costModels.hasValue() ? this.costModels.value() : [],
  );
  /** The server's named suites; the console's own lists stand in while they load. */
  private readonly suitePresets = resource({ loader: () => this.lab.survivalPresets() });
  protected readonly suites = computed(() =>
    this.suitePresets.hasValue() ? suitesFromPresets(this.suitePresets.value()) : SUITES,
  );
  /** The tests the lab run will do, with their labels. */
  protected readonly runTests = computed(() =>
    suiteTests({ suite: this.suite(), tests: this.tests() }, this.suites()).map((id) =>
      SURVIVAL_TESTS.find((t) => t.id === id)!,
    ),
  );
  protected readonly instruments = resource({
    loader: () => this.market.instruments({ limit: 500 }),
  });

  protected readonly tickers = computed(() => parseTickers(this.tickersText()));
  protected readonly tickersError = computed(() =>
    this.tickers().length ? null : 'Enter at least one ticker.',
  );
  protected readonly windowError = computed(() =>
    this.start() && this.end() && this.start() < this.end()
      ? null
      : 'The start date must be before the end date.',
  );
  protected readonly costErrors = computed(() =>
    this.submitted() ? costErrors(this.costForm()) : {},
  );
  protected readonly formValid = computed(
    () =>
      !this.tickersError() &&
      !this.windowError() &&
      !Object.keys(costErrors(this.costForm())).length,
  );
  protected readonly costNote = computed(() =>
    this.costForm().cost === 'flat' ? 'Lab runs use the default costs.' : '',
  );

  protected patchCost(p: Partial<CostForm>): void {
    this.costForm.update((f) => ({ ...f, ...p }));
  }

  protected setSuite(suite: SuiteChoice): void {
    // Starting a custom suite from the suite in view saves re-ticking its tests.
    if (suite === 'custom' && this.suite() !== 'custom') {
      this.tests.set(this.runTests().map((t) => t.id));
    }
    this.suite.set(suite);
  }

  protected jobText(h: JobHandle): string {
    const msg = h.message();
    switch (h.status()) {
      case 'running':
        return msg || 'Running…';
      case 'succeeded':
        return 'Finished.';
      case 'failed':
        return h.error() ? 'Failed.' : msg || 'Failed.';
      case 'cancelled':
        return 'Cancelled.';
      default:
        return 'Waiting for a worker…';
    }
  }

  // ---- backtest --------------------------------------------------------------
  protected readonly btRun = signal<JobHandle | null>(null);
  protected readonly btResult = signal<BacktestResult | null>(null);
  protected readonly btBusy = signal(false);

  // ---- lab run ---------------------------------------------------------------
  protected readonly labRun = signal<JobHandle | null>(null);
  protected readonly labResult = signal<LabRunView | null>(null);
  protected readonly labBusy = signal(false);

  protected readonly percent = formatPercent;

  protected isRunning(h: JobHandle | null): boolean {
    return !!h && !h.done();
  }

  protected toggleTest(id: SurvivalTestName, on: boolean): void {
    this.tests.update((t) => (on ? [...t.filter((x) => x !== id), id] : t.filter((x) => x !== id)));
  }

  protected numberValue(raw: string, fallback: number): number {
    const n = Number(raw);
    return raw.trim() !== '' && Number.isFinite(n) ? n : fallback;
  }

  private window() {
    return {
      universe: this.tickers(),
      start: this.start(),
      end: this.end(),
      interval: this.interval(),
    };
  }

  async runBacktest(): Promise<void> {
    this.submitted.set(true);
    if (!this.formValid() || this.btBusy() || !this.canLab()) return;
    this.btBusy.set(true);
    try {
      if (!(await this.ensureSaved()())) return;
      const body: DraftBacktestRequest = {
        ...this.window(),
        initial_cash: this.initialCash(),
        rebalance_every_bars: this.rebalanceEvery(),
        ...costFields(this.costForm()),
      };
      this.btResult.set(null);
      this.btRun()?.stop();
      const job = await this.studio.startBacktest(this.draft().id, body);
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.btRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const done = await this.jobsApi.get(job.id);
        this.btResult.set((done.result as BacktestResult | null) ?? null);
        this.toasts.success('Backtest finished.');
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.btBusy.set(false);
    }
  }

  async runLab(): Promise<void> {
    this.submitted.set(true);
    if (!this.formValid() || this.labBusy() || !this.runTests().length || !this.canLab()) return;
    this.labBusy.set(true);
    try {
      if (!(await this.ensureSaved()())) return;
      const suite = this.suite();
      const cost = this.costForm().cost;
      const body: DraftLabRunRequest = {
        ...this.window(),
        objective: this.objective(),
        ...(suite === 'custom'
          ? { survival_tests: this.runTests().map((t) => t.id) }
          : { preset: suite }),
        ...(cost === 'zero' || cost === 'realistic' ? { cost_model: cost } : {}),
      };
      this.labResult.set(null);
      this.labRun()?.stop();
      const job = await this.studio.startLabRun(this.draft().id, body);
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.labRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const done = await this.jobsApi.get(job.id);
        this.labResult.set((done.result as LabRunView | null) ?? null);
        this.toasts.success('Lab run finished.');
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.labBusy.set(false);
    }
  }

  async cancel(handle: JobHandle | null): Promise<void> {
    if (!handle) return;
    try {
      await this.jobsApi.cancel(handle.jobId);
      this.toasts.info('Cancel requested.');
    } catch {
      // Toasted by the interceptor (e.g. a running backtest cannot be cancelled).
    }
  }
}
