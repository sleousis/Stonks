import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  type ElementRef,
  computed,
  inject,
  input,
  resource,
  signal,
  viewChild,
} from '@angular/core';

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
}

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

/** Strategy a lab job ran, from its stored request: class name or registered id. */
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

  protected readonly mode = signal<FormKind>('backtest');

  /** Query param `?strategy=<id>`: start both forms from a registered strategy. */
  readonly strategy = input<string | undefined>();
  protected readonly registered = resource({
    params: () => {
      const id = this.strategy();
      return id ? { id } : undefined;
    },
    loader: ({ params }) => this.strategiesApi.get(params.id),
  });
  protected readonly preset = computed<StrategyPreset | null>(() =>
    this.registered.hasValue() ? presetFromStrategy(this.registered.value()) : null,
  );
  /** The preset's class is not in the catalog (e.g. a code strategy that is off). */
  protected readonly presetUncatalogued = computed(() => {
    const p = this.preset();
    if (!p || !this.classes.hasValue()) return false;
    return !this.classes.value().some((c) => c.class_path === p.classPath);
  });
  private readonly resultPanel = viewChild<ElementRef<HTMLElement>>('resultPanel');

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

  async startBacktest(request: BacktestRequest): Promise<void> {
    const name = shortName(request.strategy.class_path);
    const ok = await this.confirm.confirm({
      title: `Run a backtest of ${name}?`,
      message: `${basket(request)}, ${request.start} to ${request.end}, ${request.interval ?? '1d'} bars. It runs in the background.`,
      confirmLabel: 'Run backtest',
    });
    if (!ok) return;
    await this.start('backtest', name, () => this.lab.startBacktest(request), 'Started a backtest');
  }

  async startLabRun(request: LabRunRequest): Promise<void> {
    const name = shortName(request.strategy.class_path);
    const always = !!request.register_strategy;
    const register = always || !!request.register_if_passes;
    const suite = request.preset
      ? `the ${request.preset} suite`
      : `${request.survival_tests?.length ?? 0} survival tests`;
    const fetchFirst = request.ensure_data ? 'Fetches missing data first, then a ' : 'A ';
    const ok = await this.confirm.confirm({
      title: `Start a lab run of ${name}?`,
      message:
        `${basket(request)}. ${fetchFirst}${request.tuner ?? 'random'} search, ${request.budget ?? 20} trials, then ${suite}.` +
        (always
          ? ' The fitted strategy is registered in shadow when the run finishes, whatever the verdict.'
          : register
            ? ' The fitted strategy is registered in shadow only if every test passes.'
            : ''),
      confirmLabel: register ? 'Start and register' : 'Start lab run',
      typedConfirmation: register ? name : undefined,
    });
    if (!ok) return;
    await this.start('lab_run', name, () => this.lab.startLabRun(request), 'Started a lab run');
  }

  /** Follow a job from the history table (running or finished). */
  protected open(job: Job): void {
    void this.follow(job.id, job.kind as LabKind, jobStrategy(job));
    this.revealResult();
  }

  protected async cancel(jobId: string, kind: string, label: string): Promise<void> {
    const running = kind === 'lab_run';
    const ok = await this.confirm.confirm({
      title: `Cancel this ${kindLabel(kind).toLowerCase()}?`,
      message: running
        ? `${label} stops after the current tuning trial or survival test. Nothing is registered.`
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
    const following = this.follow(job.id, kind, label);
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

  private async follow(jobId: string, kind: LabKind, label: string): Promise<void> {
    this.followed()?.handle.stop();
    const handle = this.jobs.track(jobId, this.destroyRef);
    this.followed.set({ jobId, kind, label, handle });
    this.shown.set(null);
    this.resultError.set(null);
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
      this.resultLoading.set(false);
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
