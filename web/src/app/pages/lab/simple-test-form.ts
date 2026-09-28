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

import type { LabRunRequest, StrategyClassInfo, UniverseView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import {
  type LabRunForm,
  SUITES,
  buildLabRunRequest,
  defaultLabRunForm,
  labRunErrors,
} from './lab-requests';
import { StrategyPicker } from './strategy-picker';
import { WindowFields } from './window-fields';

/** The suite a simple test runs: thorough enough to mean something, a few minutes. */
export const SIMPLE_SUITE = 'standard';

/** The fields the simple form owns; everything else keeps the lab-run defaults. */
export type SimpleTestForm = Pick<
  LabRunForm,
  'classPath' | 'tickers' | 'universeId' | 'start' | 'end'
>;

/**
 * The request a simple test sends: a lab run with the Standard robustness
 * tests and every other setting at its default. Nothing goes on trial.
 */
export function simpleTestRequest(f: SimpleTestForm, today?: Date): LabRunRequest {
  const form: LabRunForm = {
    ...defaultLabRunForm(today),
    ...f,
    interval: '1d',
    suite: SIMPLE_SUITE,
    ensureData: !!f.universeId,
  };
  return buildLabRunRequest(form);
}

/** Field errors of a simple test, in the same words as the full form. */
export function simpleTestErrors(f: SimpleTestForm): Record<string, string> {
  const all = labRunErrors({ ...defaultLabRunForm(), ...f, suite: SIMPLE_SUITE });
  const out: Record<string, string> = {};
  for (const key of ['strategy', 'tickers', 'start', 'end'] as const) {
    const msg = all[key];
    if (msg) out[key] = msg;
  }
  return out;
}

/**
 * "Test a strategy" in three choices: which strategy, what to trade and
 * which dates. It runs the Standard robustness tests with default settings,
 * so a trader gets a plain verdict without meeting a quant setting.
 */
@Component({
  selector: 'app-simple-test-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StrategyPicker, WindowFields, PermissionNote],
  template: `
    <form class="lab-form" novalidate (submit)="$event.preventDefault(); submit()">
      <ol class="steps" aria-label="How a test works">
        <li><strong>Pick a strategy</strong> and what it should trade.</li>
        <li>
          <strong>Stonks replays it</strong> over the dates you choose and tries a few settings.
        </li>
        <li>
          <strong>Then it asks whether the result is luck</strong>, with
          {{ testCount() }} robustness tests on data the search never saw.
        </li>
      </ol>

      <app-strategy-picker
        idPrefix="st"
        [classes]="classes()"
        [value]="form().classPath"
        [error]="errors()['strategy'] ?? null"
        (valueChange)="patch({ classPath: $event })"
      />

      <fieldset class="group">
        <legend>What to trade</legend>
        @if (universes().length) {
          <div class="field">
            <label for="st-universe">Trade</label>
            <select
              id="st-universe"
              class="input"
              aria-describedby="st-universe-hint"
              (change)="patch({ universeId: $any($event.target).value })"
            >
              <option value="" [selected]="form().universeId === ''">Tickers I type</option>
              @for (u of universes(); track u.id) {
                <option [value]="u.id" [selected]="form().universeId === u.id">
                  {{ u.name || u.id }}
                </option>
              }
            </select>
            <span class="hint" id="st-universe-hint">
              A saved list includes names that were members then, even ones that later left, and any
              missing prices are fetched first.
            </span>
          </div>
        }
        <app-window-fields
          idPrefix="st"
          [value]="windowValue()"
          [errors]="errors()"
          [showTickers]="!form().universeId"
          [showInterval]="false"
          (patch)="patch($event)"
        />
      </fieldset>

      <div class="form-actions">
        <p class="hint what-runs">
          Runs the {{ suiteLabel }} tests, usually a few minutes. Nothing trades and nothing goes on
          trial.
        </p>
        <button
          type="submit"
          class="btn btn-primary"
          [disabled]="busy() || !canRun()"
          [attr.aria-busy]="busy()"
        >
          {{ busy() ? 'Starting…' : 'Test it' }}
        </button>
        <app-permission-note permission="lab.run" />
      </div>
    </form>
  `,
  styleUrl: './lab-form.scss',
  styles: `
    .steps {
      margin: 0;
      padding-left: var(--space-5);
      display: grid;
      gap: var(--space-1);
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .steps strong {
      color: var(--color-ink);
      font-weight: var(--weight-semibold);
    }
    .what-runs {
      flex: 1 1 14rem;
      margin: 0;
    }
  `,
})
export class SimpleTestFormView {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly universes = input<readonly UniverseView[]>([]);
  /** Tickers to start from (a watchlist opened in the lab). */
  readonly tickers = input<string | null>(null);
  /** A stored universe to start from (`?universe=`). */
  readonly universe = input<string | null>(null);
  /** How many tests the Standard suite runs (from the server's presets). */
  readonly testCount = input(SUITES.find((s) => s.id === SIMPLE_SUITE)?.tests.length ?? 7);
  readonly busy = input(false);
  readonly submitted = output<LabRunRequest>();

  private readonly session = inject(SessionService);
  protected readonly canRun = computed(() => this.session.can('lab.run'));
  protected readonly suiteLabel = SUITES.find((s) => s.id === SIMPLE_SUITE)?.label ?? 'Standard';

  protected readonly form = linkedSignal<
    { tickers: string | null; universe: string | null },
    SimpleTestForm
  >({
    source: () => ({ tickers: this.tickers(), universe: this.universe() }),
    computation: ({ tickers, universe }) => {
      const d = defaultLabRunForm();
      return {
        classPath: '',
        tickers: tickers ?? '',
        universeId: universe ?? '',
        start: d.start,
        end: d.end,
      };
    },
  });
  protected readonly windowValue = computed(() => ({ ...this.form(), interval: '1d' }));
  protected readonly tried = signal(false);
  protected readonly errors = computed<Record<string, string>>(() =>
    this.tried() ? simpleTestErrors(this.form()) : {},
  );

  protected patch(p: Partial<SimpleTestForm>): void {
    this.form.update((f) => ({ ...f, ...p }));
  }

  protected submit(): void {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(simpleTestErrors(this.form())).length) return;
    this.submitted.emit(simpleTestRequest(this.form()));
  }
}
