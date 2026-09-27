import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';

import type {
  BacktestRequest,
  CostModelPreset,
  IntervalInfo,
  StrategyClassInfo,
} from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ParamForm } from '../../shared/ui/param-form/param-form';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type ParamValues, defaultParamValues } from '../../shared/ui/param-form/param-spec';
import {
  type BacktestForm,
  type BenchmarkForm,
  type CostChoice,
  type WindowForm,
  backtestErrors,
  buildBacktestRequest,
  defaultBacktestForm,
} from './lab-requests';
import { BenchmarkField } from './benchmark-field';
import { StrategyPicker } from './strategy-picker';
import { type StrategyPreset, presetParamValues } from './strategy-preset';
import { WindowFields } from './window-fields';

/** Backtest one strategy class with chosen parameters; emits the request body. */
@Component({
  selector: 'app-backtest-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StrategyPicker, ParamForm, WindowFields, BenchmarkField, PermissionNote],
  templateUrl: './backtest-form.html',
  styleUrl: './lab-form.scss',
})
export class BacktestFormView {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly intervals = input<readonly IntervalInfo[]>([]);
  readonly costModels = input<readonly CostModelPreset[]>([]);
  /** A registered strategy to start from (class and parameters). */
  readonly preset = input<StrategyPreset | null>(null);
  /** Tickers to start from (a watchlist opened in the lab). */
  readonly tickers = input<string | null>(null);
  readonly busy = input(false);
  readonly submitted = output<BacktestRequest>();

  private readonly session = inject(SessionService);
  /** May this user start lab jobs? Otherwise the button is off with a note. */
  protected readonly canRun = computed(() => this.session.can('lab.run'));

  protected readonly form = linkedSignal<
    { preset: StrategyPreset | null; tickers: string | null },
    BacktestForm
  >({
    source: () => ({ preset: this.preset(), tickers: this.tickers() }),
    computation: ({ preset, tickers }) => {
      const form = { ...defaultBacktestForm(), tickers: tickers ?? '' };
      const cls = preset && this.classes().find((c) => c.class_path === preset.classPath);
      return cls
        ? { ...form, classPath: cls.class_path, params: presetParamValues(cls, preset.params) }
        : form;
    },
  });
  protected readonly tried = signal(false);

  protected readonly selected = computed(
    () => this.classes().find((c) => c.class_path === this.form().classPath) ?? null,
  );
  private readonly allErrors = computed(() => backtestErrors(this.form(), this.selected()));
  /** Shown only after a submit attempt, so a fresh form isn't all red. */
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly errorCount = computed(() => Object.keys(this.errors()).length);
  protected readonly costHint = computed(() => {
    const c = this.form().cost;
    if (c === 'configured') return 'The fees and slippage your admin set up for backtests.';
    if (c === 'flat') return 'A flat slippage on every fill and a fixed fee per trade.';
    return this.costModels().find((m) => m.name === c)?.description ?? '';
  });

  protected selectClass(classPath: string): void {
    const cls = this.classes().find((c) => c.class_path === classPath);
    this.form.update((f) => ({
      ...f,
      classPath,
      params: cls ? defaultParamValues(cls.parameters) : {},
    }));
  }

  protected patch(p: Partial<BacktestForm> | Partial<WindowForm> | Partial<BenchmarkForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setParams(params: ParamValues): void {
    this.patch({ params });
  }

  protected setCost(cost: string): void {
    this.patch({ cost: cost as CostChoice });
  }

  protected setNumber(key: keyof BacktestForm, raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ [key]: n === null || Number.isNaN(n) ? null : n });
  }

  protected resetParams(): void {
    const cls = this.selected();
    if (cls) this.patch({ params: defaultParamValues(cls.parameters) });
  }

  protected submit(): void {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    this.submitted.emit(buildBacktestRequest(this.form(), this.selected()));
  }
}
