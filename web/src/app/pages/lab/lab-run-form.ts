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
import type { IntervalInfo, LabRunRequest, StrategyClassInfo } from '../../api/models';
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

/**
 * Tune a strategy class, fit it and run a survival suite; emits the
 * request body. The page confirms before starting when "register" is on.
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
  /** A registered strategy to re-run: its class (the tuner searches the parameters again). */
  readonly preset = input<StrategyPreset | null>(null);
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

  protected readonly suites = SUITES;
  protected readonly pickable = PICKABLE_TESTS;
  protected readonly form = linkedSignal<StrategyPreset | null, LabRunForm>({
    source: this.preset,
    computation: (preset) => {
      const form = defaultLabRunForm();
      const known = preset && this.classes().some((c) => c.class_path === preset.classPath);
      return known ? { ...form, classPath: preset.classPath } : form;
    },
  });
  protected readonly tried = signal(false);
  /** The advanced options panel; opened on submit when one of its fields is wrong. */
  protected readonly optionsOpen = signal(false);

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
    suiteTests(this.form()).map((id) => SURVIVAL_TESTS.find((t) => t.id === id)!),
  );
  protected readonly suiteInfo = computed(() => SUITES.find((s) => s.id === this.form().suite)!);
  protected readonly runs = (id: SurvivalTestName) => suiteTests(this.form()).includes(id);

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
  /** Show the advanced panel while the catalog loads or failed, so the error has a place. */
  protected readonly showOptions = computed(
    () => this.optionTests().length > 0 || !this.testCatalog.hasValue(),
  );
  protected readonly optionsFilled = computed(() =>
    this.optionTests().reduce((n, t) => n + t.filled, 0),
  );

  private readonly allErrors = computed(() => labRunErrors(this.form(), this.catalog()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly errorCount = computed(() => Object.keys(this.errors()).length);
  protected readonly optionErrorCount = computed(
    () => Object.keys(this.errors()).filter((k) => k.startsWith('opt.')).length,
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
      tests: suite === 'custom' && f.suite !== 'custom' ? suiteTests(f) : f.tests,
    }));
  }

  /** Registering runs default to the promotion suite, as the API does. */
  protected setRegister(on: boolean): void {
    this.form.update((f) => ({
      ...f,
      register: on,
      suite: on && f.suite === 'quick' ? 'promotion' : f.suite,
    }));
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
    if (errors.some((k) => k.startsWith('opt.'))) this.optionsOpen.set(true);
    if (errors.length) return;
    this.submitted.emit(buildLabRunRequest(this.form(), this.catalog()));
  }
}
