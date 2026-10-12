import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  output,
  resource,
  signal,
} from '@angular/core';

import { MarketService } from '../../api/market.service';
import type {
  LabRunRequest,
  StrategyClassInfo,
  UniverseView,
  WatchlistView,
} from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import {
  type LabRunForm,
  SUITES,
  buildLabRunRequest,
  defaultLabRunForm,
  labRunErrors,
  parseTickers,
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
export function simpleTestRequest(f: SimpleTestForm, today?: Date, idea = ''): LabRunRequest {
  const form: LabRunForm = {
    ...defaultLabRunForm(today),
    ...f,
    interval: '1d',
    suite: SIMPLE_SUITE,
    ensureData: !!f.universeId,
    // The ledger shows the catalog's idea when the trader wrote none.
    hypothesis: idea,
  };
  return buildLabRunRequest(form);
}

/**
 * The latest first day of data across the tickers: the earliest start where
 * every one of them has prices. `null` when any ticker has no data.
 */
export function earliestCommonStart(
  firstBars: readonly (string | null | undefined)[],
): string | null {
  if (!firstBars.length || firstBars.some((d) => !d)) return null;
  return firstBars.map((d) => d!.slice(0, 10)).reduce((a, b) => (a > b ? a : b));
}

/** Tickers looked up for their first day of data; more than this keeps the default start. */
const MAX_COVERAGE_LOOKUPS = 20;
/** Typing pauses this long before the lookup runs. */
const COVERAGE_DEBOUNCE_MS = 400;

/** The basket choice's value for a watchlist (universes use their own id). */
const WATCHLIST = 'watchlist:';

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
        <legend>What to test it on</legend>
        @if (hasLists()) {
          <div class="field">
            <label for="st-universe">Tickers to test</label>
            <select
              id="st-universe"
              class="input"
              [attr.aria-describedby]="form().universeId ? 'st-universe-hint' : null"
              (change)="pickBasket($any($event.target).value)"
            >
              <option value="" [selected]="basket() === ''">Tickers I type</option>
              @if (watchlists().length) {
                <optgroup label="Watchlists">
                  @for (w of watchlists(); track w.id) {
                    <option
                      [value]="watchlistValue(w.id)"
                      [selected]="basket() === watchlistValue(w.id)"
                    >
                      {{ w.name }}
                    </option>
                  }
                </optgroup>
              }
              @if (universes().length) {
                <optgroup label="Universes">
                  @for (u of universes(); track u.id) {
                    <option [value]="u.id" [selected]="basket() === u.id">
                      {{ u.name || u.id }}
                    </option>
                  }
                </optgroup>
              }
            </select>
            @if (form().universeId) {
              <span class="hint" id="st-universe-hint">
                A universe includes names that were members then, even ones that later left, and any
                missing prices are fetched first.
              </span>
            }
          </div>
        }
        <app-window-fields
          idPrefix="st"
          [value]="windowValue()"
          [errors]="errors()"
          [showTickers]="!form().universeId"
          [showInterval]="false"
          [tickersLabel]="hasLists() ? 'Tickers' : 'Tickers to test'"
          (patch)="patch($event)"
        />
        @if (dataStart() && !startTouched()) {
          <p class="hint start-note">Starts on the first day with prices for every ticker.</p>
        }
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
      color: var(--ink-2);
    }
    .steps strong {
      color: var(--ink);
      font-weight: var(--weight-semibold);
    }
    .what-runs {
      flex: 1 1 14rem;
      margin: 0;
    }
    .start-note {
      margin: 0;
    }
  `,
})
export class SimpleTestFormView {
  readonly classes = input.required<readonly StrategyClassInfo[]>();
  readonly universes = input<readonly UniverseView[]>([]);
  /** The viewer's watchlists, offered next to the universes. */
  readonly watchlists = input<readonly WatchlistView[]>([]);
  /** Tickers to start from (a watchlist opened in the lab, or a strategy's own). */
  readonly tickers = input<string | null>(null);
  /** A stored universe to start from (`?universe=`). */
  readonly universe = input<string | null>(null);
  /** A strategy to start from ("Test again in Lab"). */
  readonly classPath = input<string | null>(null);
  /** How many tests the Standard suite runs (from the server's presets). */
  readonly testCount = input(SUITES.find((s) => s.id === SIMPLE_SUITE)?.tests.length ?? 7);
  readonly busy = input(false);
  readonly submitted = output<LabRunRequest>();

  private readonly session = inject(SessionService);
  private readonly market = inject(MarketService);
  protected readonly canRun = computed(() => this.session.can('lab.run'));
  protected readonly suiteLabel = SUITES.find((s) => s.id === SIMPLE_SUITE)?.label ?? 'Standard';

  protected readonly form = linkedSignal<
    { tickers: string | null; universe: string | null; classPath: string | null },
    SimpleTestForm
  >({
    source: () => ({
      tickers: this.tickers(),
      universe: this.universe(),
      classPath: this.classPath(),
    }),
    computation: ({ tickers, universe, classPath }) => {
      const d = defaultLabRunForm();
      const known = !!classPath && this.classes().some((c) => c.class_path === classPath);
      return {
        classPath: known ? classPath : '',
        tickers: tickers ?? '',
        universeId: universe ?? '',
        start: d.start,
        end: d.end,
      };
    },
  });
  /** The basket select's value: '' (typed), a watchlist or a universe id. */
  protected readonly basket = linkedSignal(() => this.universe() ?? '');
  protected readonly hasLists = computed(
    () => this.universes().length > 0 || this.watchlists().length > 0,
  );
  protected readonly watchlistValue = (id: string) => WATCHLIST + id;
  /** Set once the trader picks a start date: the data lookup no longer moves it. */
  protected readonly startTouched = signal(false);
  /** The first day every typed ticker has prices, looked up after typing pauses. */
  private readonly lookupKey = computed(() => {
    const f = this.form();
    if (this.startTouched() || f.universeId) return '';
    const tickers = parseTickers(f.tickers);
    return tickers.length <= MAX_COVERAGE_LOOKUPS ? tickers.join(',') : '';
  });
  private readonly coverage = resource({
    params: () => {
      const key = this.lookupKey();
      return key ? { tickers: key.split(',') } : undefined;
    },
    loader: async ({ params, abortSignal }) => {
      await new Promise((resolve) => setTimeout(resolve, COVERAGE_DEBOUNCE_MS));
      if (abortSignal.aborted) return null;
      const pages = await Promise.all(
        params.tickers.map((ticker) => this.market.coverage({ ticker, interval: '1d', limit: 1 })),
      );
      return earliestCommonStart(pages.map((p) => p.items[0]?.first_bar));
    },
  });
  protected readonly dataStart = computed(() =>
    this.coverage.hasValue() ? this.coverage.value() : null,
  );
  protected readonly windowValue = computed(() => ({ ...this.form(), interval: '1d' }));
  protected readonly tried = signal(false);
  protected readonly errors = computed<Record<string, string>>(() =>
    this.tried() ? simpleTestErrors(this.form()) : {},
  );

  constructor() {
    // The longest window the data allows: start on the first day with prices.
    effect(() => {
      const start = this.dataStart();
      if (start && !this.startTouched() && start < this.form().end) {
        this.form.update((f) => (f.start === start ? f : { ...f, start }));
      }
    });
  }

  protected patch(p: Partial<SimpleTestForm>): void {
    if (p.start !== undefined) this.startTouched.set(true);
    this.form.update((f) => ({ ...f, ...p }));
  }

  /** Typed tickers, a watchlist (its tickers fill the field) or a universe. */
  protected pickBasket(value: string): void {
    this.basket.set(value);
    if (value.startsWith(WATCHLIST)) {
      const list = this.watchlists().find((w) => WATCHLIST + w.id === value);
      this.form.update((f) => ({ ...f, universeId: '', tickers: list?.tickers.join(', ') ?? '' }));
      return;
    }
    this.form.update((f) => ({ ...f, universeId: value }));
  }

  protected submit(): void {
    if (!this.canRun()) return;
    this.tried.set(true);
    if (Object.keys(simpleTestErrors(this.form())).length) return;
    const idea = this.classes().find((c) => c.class_path === this.form().classPath)?.description;
    this.submitted.emit(simpleTestRequest(this.form(), undefined, idea ?? ''));
  }
}
