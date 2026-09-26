import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';

import type { IntervalInfo, LabRunRequest, StrategyClassInfo } from '../../api/models';
import { paramFields, rangeText } from '../../shared/ui/param-form/param-spec';
import {
  type LabRunForm,
  SURVIVAL_TESTS,
  type SurvivalTestName,
  type WindowForm,
  buildLabRunRequest,
  defaultLabRunForm,
  labRunErrors,
} from './lab-requests';
import { StrategyPicker } from './strategy-picker';
import type { StrategyPreset } from './strategy-preset';
import { WindowFields } from './window-fields';

/**
 * Tune a strategy class, fit it and run the survival suite; emits the
 * request body. The page confirms before starting when "register" is on.
 */
@Component({
  selector: 'app-lab-run-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StrategyPicker, WindowFields],
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

  protected readonly tests = SURVIVAL_TESTS;
  protected readonly form = linkedSignal<StrategyPreset | null, LabRunForm>({
    source: this.preset,
    computation: (preset) => {
      const form = defaultLabRunForm();
      const known = preset && this.classes().some((c) => c.class_path === preset.classPath);
      return known ? { ...form, classPath: preset.classPath } : form;
    },
  });
  protected readonly tried = signal(false);

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

  private readonly allErrors = computed(() => labRunErrors(this.form()));
  protected readonly errors = computed(() => (this.tried() ? this.allErrors() : {}));
  protected readonly errorCount = computed(() => Object.keys(this.errors()).length);
  protected readonly has = (id: SurvivalTestName) => this.form().tests.includes(id);

  protected patch(p: Partial<LabRunForm> | Partial<WindowForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected setNumber(key: keyof LabRunForm, raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.patch({ [key]: n === null || Number.isNaN(n) ? null : n });
  }

  protected setChoice<K extends 'tuner' | 'objective' | 'mcptMetric'>(key: K, value: string): void {
    this.patch({ [key]: value as LabRunForm[K] });
  }

  protected toggleTest(id: SurvivalTestName, on: boolean): void {
    this.form.update((f) => ({
      ...f,
      tests: on ? [...f.tests.filter((t) => t !== id), id] : f.tests.filter((t) => t !== id),
    }));
  }

  protected submit(): void {
    this.tried.set(true);
    if (Object.keys(this.allErrors()).length) return;
    this.submitted.emit(buildLabRunRequest(this.form()));
  }
}
