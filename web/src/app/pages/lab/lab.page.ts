import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  type ElementRef,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';

import { RouterLink } from '@angular/router';

import { JobsApiService } from '../../api/jobs-api.service';
import { LabService } from '../../api/lab.service';
import type {
  BacktestRequest,
  BacktestResult,
  Job,
  LabRunRequest,
  LabRunView,
  SignalIcView,
  SweepResultView,
} from '../../api/models';
import { SignalsService } from '../../api/signals.service';
import { StrategiesService } from '../../api/strategies.service';
import { SystemService } from '../../api/system.service';
import { UniversesService } from '../../api/universes.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService, isTerminal } from '../../core/jobs/jobs.service';
import { formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PctPipe } from '../../shared/format.pipes';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { BacktestFormView } from './backtest-form';
import { BacktestResultView } from '../../shared/lab-results/backtest-result';
import { LabRunFormView } from './lab-run-form';
import { LabRunResultView } from '../../shared/lab-results/lab-run-result';
import { SignalIcResult } from '../../shared/lab-results/signal-ic-result';
import { testLabel } from '../../shared/lab-results/survival-tests';
import { type LabRunForm, SUITES, formFromRequest } from './lab-requests';
import { SweepResult } from '../../shared/lab-results/sweep-result';
import { LabNav } from './lab-nav';
import { type StrategyPreset, presetFromStrategy } from './strategy-preset';

/** The run types the forms on this screen start. */
export type FormKind = 'backtest' | 'lab_run';
/** Every job kind the history lists and the result panel can show. */
export type LabKind = FormKind | 'lab_sweep' | 'signal_ic';
const LAB_KINDS: readonly LabKind[] = ['backtest', 'lab_run', 'lab_sweep', 'signal_ic'];
const HISTORY_SIZE = 20;

/** The job the result panel follows: one just started, or one opened from history. */
interface Followed {
  jobId: string;
  kind: LabKind;
  label: string;
  handle: JobHandle;
  /** The request the job ran (a job's stored `params`), for a prefilled re-run. */
  params: Readonly<Record<string, unknown>> | null;
}

/** What to do after a finished lab run (UX-22). */
export type NextStep =
  | { kind: 'paper'; strategyId: string }
  | { kind: 'passed' }
  | { kind: 'failed'; failed: string[]; total: number };

export function nextStep(result: LabRunView): NextStep {
  if (result.registered_strategy_id) {
    return { kind: 'paper', strategyId: result.registered_strategy_id };
  }
  if (result.verdict === 'pass') return { kind: 'passed' };
  return {
    kind: 'failed',
    failed: result.survival_reports.filter((r) => !r.passed).map((r) => testLabel(r.test_id)),
    total: result.survival_reports.length,
  };
}

const SUITE_PARAMS = new Set(['quick', 'standard', 'promotion']);

type Shown =
  | { kind: 'backtest'; jobId: string; result: BacktestResult }
  | { kind: 'lab_run'; jobId: string; result: LabRunView }
  | { kind: 'lab_sweep'; jobId: string; result: SweepResultView }
  | { kind: 'signal_ic'; jobId: string; result: SignalIcView };

const KIND_LABELS: Record<string, string> = {
  backtest: 'Backtest',
  lab_run: 'Lab run',
  lab_sweep: 'Sweep',
  signal_ic: 'Signal IC',
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** Strategy a lab job ran, from its stored request: class name or strategy id. */
export function jobStrategy(job: Pick<Job, 'params'>): string {
  if (!('strategy' in job.params) && 'start' in job.params) {
    // A sweep: its strategy list, or every strategy.
    const list = job.params['strategies'] as string[] | null | undefined;
    if (!list?.length) return 'All strategies';
    return list.length === 1 ? (list[0].split(':').at(-1) ?? list[0]) : `${list.length} strategies`;
  }
  const ref = job.params['strategy'] as
    { class_path?: string | null; strategy_id?: string | null } | undefined;
  if (ref?.strategy_id) return ref.strategy_id;
  if (ref?.class_path) return ref.class_path.split(':').at(-1) ?? ref.class_path;
  return '–';
}

/** Queued jobs can always be cancelled; running ones only for lab runs (cooperative). */
export function canCancel(kind: string, status: string | null | undefined): boolean {
  return status === 'queued' || (status === 'running' && kind === 'lab_run');
}

@Component({
  selector: 'app-lab-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PctPipe,
    PageHeader,
    StatusPill,
    DataTable,
    TableCell,
    LoadingState,
    EmptyState,
    ErrorState,
    BacktestFormView,
    LabRunFormView,
    BacktestResultView,
    LabRunResultView,
    SweepResult,
    SignalIcResult,
    LabNav,
    RouterLink,
  ],
  templateUrl: './lab.page.html',
  styleUrl: './lab.page.scss',
})
export class LabPage {
  private readonly lab = inject(LabService);
  private readonly signals = inject(SignalsService);
  private readonly session = inject(SessionService);
  /** Starting and cancelling lab jobs needs `lab.run`. */
  protected readonly canRun = computed(() => this.session.can('lab.run'));
  private readonly system = inject(SystemService);
  private readonly universesApi = inject(UniversesService);
  private readonly strategiesApi = inject(StrategiesService);
  private readonly jobsApi = inject(JobsApiService);
  private readonly jobs = inject(JobsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly destroyRef = inject(DestroyRef);

  /** Query param `?strategy=<id>`: start both forms from a saved strategy. */
  readonly strategy = input<string | undefined>();
  /**
   * `?preset=promotion` (or quick, standard): open the lab-run form on that
   * suite, e.g. from a go-live check's fix link.
   */
  readonly preset = input<string | undefined>();
  private readonly suiteParam = computed(() => {
    const p = this.preset();
    return p && SUITE_PARAMS.has(p) ? (p as LabRunForm['suite']) : null;
  });
  /**
   * `?universe=<id>`: open the lab-run form with that stored universe picked,
   * e.g. from a universe's page or a screen saved as a universe.
   */
  readonly universe = input<string | undefined>();
  protected readonly mode = linkedSignal<FormKind>(() =>
    this.suiteParam() || this.universe() ? 'lab_run' : 'backtest',
  );
  /** Fields the lab-run form starts from: the query's suite and universe, or a re-run. */
  protected readonly prefill = linkedSignal<Partial<LabRunForm> | null>(() => {
    const suite = this.suiteParam();
    const universeId = this.universe();
    if (!suite && !universeId) return null;
    return { ...(suite ? { suite } : {}), ...(universeId ? { universeId } : {}) };
  });
  /** `?tickers=AAPL.US,MSFT.US`: a watchlist opened in the lab. */
  readonly tickers = input<string | undefined>();
  protected readonly tickerList = computed(() => {
    const raw = this.tickers();
    return raw
      ? raw
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean)
          .join(', ')
      : null;
  });
  protected readonly sourceStrategy = resource({
    params: () => {
      const id = this.strategy();
      return id ? { id } : undefined;
    },
    loader: ({ params }) => this.strategiesApi.get(params.id),
  });
  protected readonly strategyPreset = computed<StrategyPreset | null>(() =>
    this.sourceStrategy.hasValue() ? presetFromStrategy(this.sourceStrategy.value()) : null,
  );
  /** The preset's class is not in the catalog (e.g. a code strategy that is off). */
  protected readonly presetUncatalogued = computed(() => {
    const p = this.strategyPreset();
    if (!p || !this.classes.hasValue()) return false;
    return !this.classes.value().some((c) => c.class_path === p.classPath);
  });
  private readonly resultPanel = viewChild<ElementRef<HTMLElement>>('resultPanel');
  private readonly formPanel = viewChild<ElementRef<HTMLElement>>('formPanel');

  // Reference data --------------------------------------------------------
  protected readonly classes = resource({ loader: () => this.system.strategyClasses() });
  protected readonly intervals = resource({ loader: () => this.system.intervals() });
  protected readonly costModels = resource({ loader: () => this.lab.costModels() });
  /** Stored universes the lab run can use instead of typed tickers. */
  private readonly universes = resource({ loader: () => this.universesApi.list() });
  protected readonly universeList = computed(() =>
    this.universes.hasValue() ? this.universes.value() : [],
  );
  protected readonly intervalList = computed(() =>
    this.intervals.hasValue() ? this.intervals.value() : [],
  );
  protected readonly costModelList = computed(() =>
    this.costModels.hasValue() ? this.costModels.value() : [],
  );

  // History ---------------------------------------------------------------
  protected readonly history = resource({
    loader: async () => {
      const pages = await Promise.all(
        LAB_KINDS.map((kind) => this.jobsApi.list({ kind, limit: HISTORY_SIZE })),
      );
      return pages
        .flatMap((p) => p.items)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, HISTORY_SIZE);
    },
  });

  protected readonly historyColumns: TableColumn<Job>[] = [
    { key: 'created_at', label: 'Started', format: 'datetime', mobile: 'title' },
    { key: 'kind', label: 'Kind', value: (j) => kindLabel(j.kind) },
    { key: 'strategy', label: 'Strategy', value: (j) => jobStrategy(j) },
    { key: 'status', label: 'Status' },
    {
      key: 'progress',
      label: 'Progress',
      value: (j) => formatPercent(j.progress, { digits: 0 }),
      align: 'end',
      sortable: false,
      mobile: 'hide',
    },
    { key: 'actions', label: 'Actions', sortable: false, align: 'end' },
  ];
  protected readonly jobKey = (j: Job) => j.id;

  // Current job and result -------------------------------------------------
  protected readonly starting = signal(false);
  protected readonly followed = signal<Followed | null>(null);
  protected readonly shown = signal<Shown | null>(null);
  protected readonly resultLoading = signal(false);
  protected readonly resultError = signal<unknown>(null);
  protected readonly cancelling = signal<string | null>(null);

  protected readonly followedCancellable = computed(() => {
    const f = this.followed();
    return (
      this.canRun() && !!f && !f.handle.done() && canCancel(f.kind, f.handle.status() ?? 'queued')
    );
  });

  /** The next step under a finished lab run's result. */
  protected readonly next = computed<NextStep | null>(() => {
    const s = this.shown();
    return s?.kind === 'lab_run' ? nextStep(s.result) : null;
  });
  /** A re-run can be prefilled only when the job's request is known. */
  protected readonly canRerun = computed(() => !!this.followed()?.params);

  protected readonly kindLabel = kindLabel;
  protected readonly jobStrategy = jobStrategy;
  protected readonly canCancel = canCancel;
  protected readonly isTerminal = isTerminal;

  protected selectMode(mode: FormKind, focus = false): void {
    this.mode.set(mode);
    if (focus) document.getElementById(`lab-tab-${mode}`)?.focus();
  }

  protected onTabKey(event: KeyboardEvent): void {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    this.selectMode(this.mode() === 'backtest' ? 'lab_run' : 'backtest', true);
  }

  /** A backtest is research: it starts at once, no confirmation (UX-29). */
  async startBacktest(request: BacktestRequest): Promise<void> {
    const name = shortName(request.strategy.class_path);
    await this.start(
      'backtest',
      name,
      () => this.lab.startBacktest(request),
      'Started a backtest',
      request,
    );
  }

  /**
   * A lab run starts at once. When it may start paper trading, a plain
   * confirmation says so first: paper trading places no real orders, so no
   * typed words (those are for overrides and real money).
   */
  async startLabRun(request: LabRunRequest): Promise<void> {
    const name = shortName(request.strategy.class_path);
    const always = !!request.register_strategy;
    if (always || request.register_if_passes) {
      const suiteLabel = SUITES.find((s) => s.id === request.preset)?.label;
      const suite = suiteLabel
        ? `the ${suiteLabel} suite`
        : `${request.survival_tests?.length ?? 0} survival tests`;
      const fetchFirst = request.ensure_data ? 'Fetches missing data first, then runs ' : 'Runs ';
      const ok = await this.confirm.confirm({
        title: always
          ? `Start paper trading ${name} whatever the verdict?`
          : `Start paper trading ${name} if it passes?`,
        message:
          `${fetchFirst}${suite} on ${basket(request)}. ` +
          (always
            ? 'When the run finishes, the fitted strategy trades on paper even if a test failed.'
            : 'If every test passes, the fitted strategy trades on paper.') +
          ' It decides on every trading run and places no real orders.',
        confirmLabel: 'Start the run',
      });
      if (!ok) return;
    }
    await this.start(
      'lab_run',
      name,
      () => this.lab.startLabRun(request),
      'Started a lab run',
      request,
    );
  }

  /**
   * Fill the lab-run form from the followed run's request and bring it into
   * view. `startPaper` ticks "Start paper trading if it passes" (on the
   * go-live suite when the run used the quick one).
   */
  protected rerun(startPaper: boolean): void {
    const params = this.followed()?.params;
    if (!params) return;
    const form = formFromRequest(params);
    if (startPaper) {
      form.register = true;
      form.registerIfPasses = true;
      if (!form.suite || form.suite === 'quick') form.suite = 'promotion';
    }
    this.prefill.set(form);
    this.mode.set('lab_run');
    const el = this.formPanel()?.nativeElement;
    el?.scrollIntoView?.({ block: 'start' });
    el?.querySelector<HTMLElement>('#lab-tab-lab_run')?.focus();
    this.toasts.info('The lab run form is filled in. Check it, then start the run.');
  }

  /** Follow a job from the history table (running or finished). */
  protected open(job: Job): void {
    void this.follow(job.id, job.kind as LabKind, jobStrategy(job), job.params);
    this.revealResult();
  }

  protected async cancel(jobId: string, kind: string, label: string): Promise<void> {
    const running = kind === 'lab_run';
    const ok = await this.confirm.confirm({
      title: `Cancel this ${kindLabel(kind).toLowerCase()}?`,
      message: running
        ? `${label} stops after the current tuning trial or survival test. Nothing starts paper trading.`
        : `${label} has not started yet and will not run.`,
      confirmLabel: 'Cancel job',
      cancelLabel: 'Keep running',
      tone: 'danger',
    });
    if (!ok) return;
    this.cancelling.set(jobId);
    try {
      await this.jobsApi.cancel(jobId);
      this.toasts.success(`Cancelled the ${kindLabel(kind).toLowerCase()} of ${label}.`);
      this.history.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.cancelling.set(null);
    }
  }

  protected reloadResult(): void {
    const f = this.followed();
    if (f) void this.loadResult(f.jobId, f.kind);
  }

  private async start(
    kind: LabKind,
    label: string,
    call: () => Promise<Job>,
    verb: string,
    request: BacktestRequest | LabRunRequest,
  ): Promise<void> {
    this.starting.set(true);
    let job: Job;
    try {
      job = await call();
    } catch {
      return; // toasted by the error interceptor
    } finally {
      this.starting.set(false);
    }
    this.toasts.success(`${verb} of ${label}.`);
    this.history.reload();
    const following = this.follow(
      job.id,
      kind,
      label,
      request as unknown as Readonly<Record<string, unknown>>,
    );
    this.revealResult();
    await following;
  }

  /** On one-column layouts the result sits below the form: bring it into view. */
  private revealResult(): void {
    const el = this.resultPanel()?.nativeElement;
    if (!el?.scrollIntoView) return;
    const top = el.getBoundingClientRect().top;
    if (top >= 0 && top < window.innerHeight * 0.6) return;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    el.scrollIntoView({ block: 'start', behavior: reduce ? 'auto' : 'smooth' });
  }

  private async follow(
    jobId: string,
    kind: LabKind,
    label: string,
    params: Readonly<Record<string, unknown>> | null,
  ): Promise<void> {
    this.followed()?.handle.stop();
    const handle = this.jobs.track(jobId, this.destroyRef);
    this.followed.set({ jobId, kind, label, handle, params });
    this.shown.set(null);
    this.resultError.set(null);
    // A result still loading for the previous job is not this one's.
    this.resultLoading.set(false);
    const last = await handle.finished;
    if (this.followed()?.jobId !== jobId) return;
    this.history.reload();
    if (last?.status === 'succeeded') await this.loadResult(jobId, kind);
  }

  private async loadResult(jobId: string, kind: LabKind): Promise<void> {
    this.resultLoading.set(true);
    this.resultError.set(null);
    try {
      let shown: Shown;
      switch (kind) {
        case 'backtest':
          shown = { kind, jobId, result: await this.lab.backtestResult(jobId) };
          break;
        case 'lab_run':
          shown = { kind, jobId, result: await this.lab.labRunResult(jobId) };
          break;
        case 'lab_sweep':
          shown = { kind, jobId, result: await this.lab.sweepResult(jobId) };
          break;
        case 'signal_ic':
          shown = { kind, jobId, result: await this.signals.signalIcResult(jobId) };
          break;
      }
      if (this.followed()?.jobId === jobId) this.shown.set(shown);
    } catch (err) {
      if (this.followed()?.jobId === jobId) this.resultError.set(err);
    } finally {
      // A stale load never clears the spinner of the job opened since (UX-62).
      if (this.followed()?.jobId === jobId) this.resultLoading.set(false);
    }
  }
}

function shortName(classPath: string | null | undefined): string {
  return classPath?.split(':').at(-1) ?? 'the strategy';
}

/** "3 tickers" or "the sp500 universe", for confirmations. */
export function basket(request: { universe?: string[]; universe_id?: string | null }): string {
  if (request.universe_id) return `the ${request.universe_id} universe`;
  const n = request.universe?.length ?? 0;
  return `${n} ticker${n === 1 ? '' : 's'}`;
}
