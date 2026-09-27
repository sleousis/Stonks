import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  resource,
  signal,
} from '@angular/core';

import { LabService } from '../../api/lab.service';
import type {
  IntervalInfo,
  LabRunRequest,
  StrategyClassInfo,
  UniverseView,
} from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { HelpTip } from '../../shared/ui/help-tip';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { paramFields, rangeText } from '../../shared/ui/param-form/param-spec';
import { BenchmarkField } from './benchmark-field';
import {
  type BenchmarkForm,
  type LabRunForm,
  PICKABLE_TESTS,
  SUITES,
  SURVIVAL_TESTS,
  type SuiteChoice,
  type SurvivalTestName,
  type WindowForm,
  buildLabRunRequest,
  defaultLabRunForm,
  labRunErrors,
  suiteTests,
  suitesFromPresets,
} from './lab-requests';
import { StrategyPicker } from './strategy-picker';
import type { StrategyPreset } from './strategy-preset';
import { filledCount, optionCatalog } from './test-options';
import { WindowFields } from './window-fields';

/**
 * Tests whose options have their own section in this form (and their own
 * request field), so the generic editor leaves them out.
 */
const OWN_SECTION: readonly SurvivalTestName[] = ['mcpt'];

/** Error keys of the fields inside the Advanced fold. */
const ADVANCED_FIELDS = new Set([
  'budget',
  'seed',
  'trainRatio',
  'embargoBars',
  'benchmarkTicker',
  'premortem',
  'wfSplits',
  'wfTestDays',
  'wfMinWfe',
  'mcptPermutations',
  'mcptMaxP',
]);

export function isAdvancedField(key: string): boolean {
  return ADVANCED_FIELDS.has(key) || key.startsWith('opt.');
}

/**
 * Tune a strategy class, fit it and run a survival suite; emits the
 * request body. Strategy, data, suite, hypothesis and "Start paper trading
 * if it passes" stay in view; the quant settings sit in one Advanced fold.
 */
@Component({
  selector: 'app-lab-run-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    StrategyPicker,
    WindowFields,
    BenchmarkField,
    HelpTip,
    PermissionNote,
    ErrorState,
    LoadingState,
  ],
  templateUrl: './lab-run-form.html',
  styleUrl: './lab-form.scss',
})
export class LabRunFormView {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly intervals = input<readonly IntervalInfo[]>([]);
  /** Stored universes a run may use instead of typed tickers. */
  readonly universes = input<readonly UniverseView[]>([]);
  /** A registered strategy to re-run: its class (the tuner searches the parameters again). */
  readonly preset = input<StrategyPreset | null>(null);
  /** Tickers to start from (a watchlist opened in the lab). */
  readonly tickers = input<string | null>(null);
  /**
   * Fields to start from, over the defaults: a prefilled re-run of a finished
   * run, or `?preset=promotion` from a go-live check. A new object refills the form.
   */
  readonly prefill = input<Partial<LabRunForm> | null>(null);
  readonly busy = input(false);
  readonly submitted = output<LabRunRequest>();

  private readonly lab = inject(LabService);
  private readonly session = inject(SessionService);
  /** May this user start lab jobs? Otherwise the button is off with a note. */
  protected readonly canRun = computed(() => this.session.can('lab.run'));

  /** Each survival test's options, as JSON Schema from the API. */
  protected readonly testCatalog = resource({ loader: () => this.lab.survivalTests() });
  private readonly catalog = computed(() =>
    this.testCatalog.hasValue() ? optionCatalog(this.testCatalog.value(), OWN_SECTION) : {},
  );

  /** The server's named suites; the console's own lists stand in while they load. */
  private readonly presets = resource({ loader: () => this.lab.survivalPresets() });
  protected readonly suites = computed(() =>
    this.presets.hasValue() ? suitesFromPresets(this.presets.value()) : SUITES,
  );
  protected readonly pickable = PICKABLE_TESTS;
  protected readonly form = linkedSignal<
    {
      preset: StrategyPreset | null;
      tickers: string | null;
      prefill: Partial<LabRunForm> | null;
    },
    LabRunForm
  >({
    source: () => ({ preset: this.preset(), tickers: this.tickers(), prefill: this.prefill() }),
    computation: ({ preset, tickers, prefill }) => {
      let form = { ...defaultLabRunForm(), tickers: tickers ?? '' };
      const known = preset && this.classes().some((c) => c.class_path === preset.classPath);
      if (known) form = { ...form, classPath: preset.classPath };
      return prefill ? { ...form, ...prefill } : form;
    },
  });
  protected readonly tried = signal(false);
  /** The Advanced fold; opened on submit when one of its fields is wrong. */
  protected readonly advancedOpen = signal(false);

  protected readonly selected = computed(
    () => this.classes().find((c) => c.class_path === this.form().classPath) ?? null,
  );
  /** The search space the tuner explores, read-only. */
  protected readonly space = computed(() =>
    paramFields(this.selected()?.parameters ?? []).map((f) => ({
      name: f.name,
      label: f.label,
      tunable: f.tunable,
      range:
        f.control === 'choice'
          ? f.choices.join(' / ')
          : (rangeText(f) ?? (f.control === 'bool' ? 'true / false' : String(f.defaultValue))),
    })),
  );
  protected readonly tunableCount = computed(() => this.space().filter((p) => p.tunable).length);

  /** The tests this run will do, with their labels. */
  protected readonly suiteTests = computed(() =>
    suiteTests(this.form(), this.suites()).map((id) => SURVIVAL_TESTS.find((t) => t.id === id)!),
  );
  protected readonly suiteInfo = computed(() =>
    this.suites().find((s) => s.id === this.form().suite)!,
  );
  protected readonly runs = (id: SurvivalTestName) =>
    suiteTests(this.form(), this.suites()).includes(id);

  /** Tests in the suite that take advanced options. */
  protected readonly optionTests = computed(() =>
    this.suiteTests()
      .filter((t) => this.catalog()[t.id]?.length)
      .map((t) => ({
        ...t,
        fields: this.catalog()[t.id],
        filled: filledCount(this.form().testOptions, t.id),
      })),
  );
  protected readonly optionsFilled = computed(() =>
    this.optionTests().reduce((n, t) => n + t.filled, 0),
  );

  private readonly allErrors = computed(() =>
    labRunErrors(this.form(), this.catalog(), this.suites()),
  );
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly errorCount = computed(() => Object.keys(this.errors()).length);
  protected readonly advancedErrorCount = computed(
    () => Object.keys(this.errors()).filter(isAdvancedField).length,
  );
  protected readonly has = (id: SurvivalTestName) => this.form().tests.includes(id);

  protected patch(p: Partial<LabRunForm> | Partial<WindowForm> | Partial<BenchmarkForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setNumber(key: keyof LabRunForm, raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ [key]: n === null || Number.isNaN(n) ? null : n });
  }

  protected setChoice<K extends 'tuner' | 'objective' | 'mcptMetric' | 'mcptRetune'>(
    key: K,
    value: string,
  ): void {
    this.patch({ [key]: value as LabRunForm[K] });
  }

  protected setSuite(suite: SuiteChoice): void {
    // Starting a custom suite from the preset in view saves re-ticking its tests.
    this.form.update((f) => ({
      ...f,
      suite,
      tests: suite === 'custom' && f.suite !== 'custom' ? suiteTests(f, this.suites()) : f.tests,
    }));
  }

  /** Starting paper trading defaults to the go-live suite, as the API does. */
  protected setRegister(on: boolean): void {
    this.form.update((f) => ({
      ...f,
      register: on,
      registerIfPasses: true,
      suite: on && f.suite === 'quick' ? 'promotion' : f.suite,
    }));
  }

  /** "Whatever the verdict" (Advanced): start paper trading even when a test fails. */
  protected setRegisterAlways(on: boolean): void {
    if (on) {
      this.setRegister(true);
      this.patch({ registerIfPasses: false });
    } else {
      this.patch({ registerIfPasses: true });
    }
  }

  /** '' runs on the typed tickers; fetching missing data is only for a stored universe. */
  protected setUniverse(universeId: string): void {
    this.form.update((f) => ({ ...f, universeId, ensureData: universeId ? f.ensureData : false }));
  }

  protected toggleTest(id: SurvivalTestName, on: boolean): void {
    this.form.update((f) => ({
      ...f,
      tests: on ? [...f.tests.filter((t) => t !== id), id] : f.tests.filter((t) => t !== id),
    }));
  }

  protected optionValue(test: string, key: string): string {
    return this.form().testOptions[test]?.[key] ?? '';
  }

  protected setOption(test: string, key: string, value: string): void {
    this.form.update((f) => ({
      ...f,
      testOptions: { ...f.testOptions, [test]: { ...f.testOptions[test], [key]: value } },
    }));
  }

  protected clearOptions(test: string): void {
    this.form.update((f) => {
      const next = { ...f.testOptions };
      delete next[test];
      return { ...f, testOptions: next };
    });
  }

  protected submit(): void {
    if (!this.canRun()) return;
    this.tried.set(true);
    const errors = Object.keys(this.allErrors());
    if (errors.some(isAdvancedField)) this.advancedOpen.set(true);
    if (errors.length) return;
    this.submitted.emit(buildLabRunRequest(this.form(), this.catalog(), this.suites()));
  }
}
