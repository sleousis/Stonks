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
  CostModelPreset,
  Draft,
  DraftBacktestRequest,
  DraftLabRunRequest,
  LabRunView,
} from '../../api/models';
import { StudioService } from '../../api/studio.service';
import { formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { BacktestResultView } from '../../shared/lab-results/backtest-result';
import { LabRunResultView } from '../../shared/lab-results/lab-run-result';
import { StatusPill } from '../../shared/ui/status-pill';
import { INTERVALS } from './rule-spec';

export type CostChoice = 'zero' | 'realistic' | 'custom';
type SurvivalTest = NonNullable<DraftLabRunRequest['survival_tests']>[number];

export const SURVIVAL_TESTS: readonly { id: SurvivalTest; label: string; hint: string }[] = [
  { id: 'oos', label: 'Out of sample', hint: 'Holds up on data it was not tuned on' },
  { id: 'period_stability', label: 'Period stability', hint: 'Works across sub-periods' },
  { id: 'perturbation', label: 'Perturbation', hint: 'Survives small parameter changes' },
  { id: 'drift', label: 'Drift', hint: 'Recent behaviour matches the past' },
  { id: 'runs_test', label: 'Runs test', hint: 'Wins and losses are not clustered' },
  { id: 'permutation', label: 'Permutation', hint: 'Beats shuffled prices' },
  { id: 'walk_forward', label: 'Walk forward', hint: 'Re-tuned windows keep working' },
];

/** Slippage (bps) and flat fee a cost preset amounts to for one asset class. */
export function costsFor(
  preset: CostModelPreset | undefined,
  assetClass: string,
): { slippage_bps: number; fee_per_trade: number } {
  if (!preset) return { slippage_bps: 0, fee_per_trade: 0 };
  const s = preset.settings;
  const c = s.asset_classes?.[assetClass] ?? s.default ?? {};
  return {
    slippage_bps: round2((c.half_spread_bps ?? 0) + (c.fee_bps ?? 0) + (s.impact_bps ?? 0)),
    fee_per_trade: round2(c.fee_flat ?? 0),
  };
}

export function parseTickers(raw: string): string[] {
  return [
    ...new Set(
      raw
        .split(/[\s,;]+/)
        .map((t) => t.trim().toUpperCase())
        .filter(Boolean),
    ),
  ];
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/**
 * Test a draft: a backtest (equity, drawdown, metrics) and a lab run
 * (survival verdicts), each a background job followed live. Unsaved builder
 * changes are saved first through `ensureSaved`.
 */
@Component({
  selector: 'app-draft-test',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BacktestResultView, LabRunResultView, StatusPill],
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

  readonly draft = input.required<Draft>();
  /** Interval and allow-list of the spec being edited (defaults for the form). */
  readonly specInterval = input('1d');
  readonly specTickers = input<readonly string[] | null>(null);
  readonly assetClass = input('equity');
  /** Saves pending edits; resolves false when the draft could not be saved. */
  readonly ensureSaved = input<() => Promise<boolean>>(() => Promise.resolve(true));

  protected readonly intervals = INTERVALS;
  protected readonly survivalTests = SURVIVAL_TESTS;

  // ---- setup form ------------------------------------------------------------
  protected readonly tickersText = linkedSignal(() => (this.specTickers() ?? []).join(', '));
  protected readonly start = signal(isoDay(new Date(Date.now() - 365 * 86_400_000)));
  protected readonly end = signal(isoDay(new Date()));
  protected readonly interval = linkedSignal(() => this.specInterval());
  protected readonly initialCash = signal(10_000);
  protected readonly rebalanceEvery = signal(1);
  protected readonly cost = signal<CostChoice>('zero');
  protected readonly customSlippage = signal(5);
  protected readonly customFee = signal(0);
  protected readonly tests = signal<SurvivalTest[]>(['oos', 'period_stability']);
  protected readonly objective = signal<'sharpe' | 'cagr' | 'final_return'>('sharpe');
  protected readonly submitted = signal(false);

  protected readonly costModels = resource({ loader: () => this.lab.costModels() });
  protected readonly instruments = resource({
    loader: () => this.market.instruments({ limit: 500 }),
  });

  protected readonly tickers = computed(() => parseTickers(this.tickersText()));
  protected readonly tickersError = computed(() =>
    this.tickers().length ? null : 'Enter at least one ticker from the lake.',
  );
  protected readonly windowError = computed(() =>
    this.start() && this.end() && this.start() < this.end()
      ? null
      : 'The start date must be before the end date.',
  );
  protected readonly formValid = computed(() => !this.tickersError() && !this.windowError());

  protected readonly costs = computed(() => {
    const choice = this.cost();
    if (choice === 'custom') {
      return { slippage_bps: this.customSlippage(), fee_per_trade: this.customFee() };
    }
    const presets = this.costModels.hasValue() ? this.costModels.value() : [];
    return costsFor(
      presets.find((p) => p.name === choice),
      this.assetClass(),
    );
  });
  protected readonly costSummary = computed(() => {
    const c = this.costs();
    return `${formatNumber(c.slippage_bps)} bps slippage, ${formatMoney(c.fee_per_trade)} per trade`;
  });
  protected readonly costHint = computed(() => {
    const summary = this.costSummary();
    if (this.cost() !== 'realistic') return `${summary}.`;
    // Draft backtests take flat slippage and fees, so the preset's spread and
    // percentage fee are folded into slippage; size-dependent impact is not.
    return `≈ ${summary}, from the realistic preset's spread and fees for ${this.assetClass()}.`;
  });

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

  protected toggleTest(id: SurvivalTest, on: boolean): void {
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
    if (!this.formValid() || this.btBusy()) return;
    this.btBusy.set(true);
    try {
      if (!(await this.ensureSaved()())) return;
      const body: DraftBacktestRequest = {
        ...this.window(),
        initial_cash: this.initialCash(),
        rebalance_every_bars: this.rebalanceEvery(),
        ...this.costs(),
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
    if (!this.formValid() || this.labBusy() || !this.tests().length) return;
    this.labBusy.set(true);
    try {
      if (!(await this.ensureSaved()())) return;
      const body: DraftLabRunRequest = {
        ...this.window(),
        objective: this.objective(),
        survival_tests: this.tests(),
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
