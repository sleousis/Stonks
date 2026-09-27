import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  effect,
  inject,
  input,
  linkedSignal,
  output,
  signal,
  untracked,
} from '@angular/core';

import { FactorsService } from '../../../api/factors.service';
import type { ExpressionCheckView } from '../../../api/models';
import { warmupText } from './factor-requests';

/** How long typing must pause before the formula is checked. */
export const CHECK_DELAY_MS = 350;

/** The fields and functions of the expression language, for the help line. */
export const FORMULA_FIELDS = ['$open', '$high', '$low', '$close', '$volume'] as const;
export const FORMULA_FUNCTIONS = [
  'Ref',
  'Mean',
  'Std',
  'Sum',
  'Max',
  'Min',
  'Rank',
  'CSRank',
  'Corr',
  'Slope',
  'Rsquare',
  'Resi',
  'Quantile',
  'IdxMax',
  'IdxMin',
  'Delta',
  'Log',
  'Abs',
  'Sign',
  'Greater',
  'Less',
  'If',
] as const;

/**
 * A factor formula with a live check: parses, point in time, no raw price
 * levels. Shows the canonical form and the warm-up in bars. Emits the
 * formula once it checks out, and `null` while it does not.
 *
 *   <app-formula-editor [initial]="expr" (valid)="formula.set($event)" />
 */
@Component({
  selector: 'app-formula-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="field">
      <label for="formula-text">Formula</label>
      <textarea
        id="formula-text"
        class="input mono"
        rows="3"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        placeholder="$close / Ref($close, 20) - 1"
        aria-describedby="formula-status formula-help"
        [attr.aria-invalid]="check()?.ok === false"
        [value]="text()"
        (input)="edit($any($event.target).value)"
      ></textarea>
    </div>

    <div id="formula-status" class="status" role="status" aria-live="polite">
      @if (!text().trim()) {
        <span class="muted">Type a formula to check it.</span>
      } @else if (checking()) {
        <span class="muted">Checking the formula</span>
      } @else if (failed()) {
        <span class="bad">Could not check the formula right now. Try again in a moment.</span>
      } @else if (check(); as c) {
        @if (c.ok) {
          <span class="good">Looks good.</span>
          <dl class="facts">
            <div>
              <dt>Reads as</dt>
              <dd class="mono">{{ c.canonical }}</dd>
            </div>
            <div>
              <dt>Warm-up</dt>
              <dd class="num">{{ warmup(c.lookback_bars) }}</dd>
            </div>
          </dl>
        } @else {
          <span class="bad">{{ c.error || 'This formula does not work.' }}</span>
        }
      }
    </div>

    <details id="formula-help" class="help">
      <summary>Fields and functions</summary>
      <p>
        Fields: <span class="mono">{{ fields }}</span>
      </p>
      <p>
        Functions: <span class="mono">{{ functions }}</span>
      </p>
      <p>
        A formula may not read ahead: Ref with a negative offset is refused. Compare prices as
        ratios, such as $close / Ref($close, 20), so a later split does not change the value.
      </p>
    </details>
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }
    .mono {
      font-family: var(--font-mono);
      overflow-wrap: anywhere;
    }
    .status {
      display: grid;
      gap: var(--space-2);
      min-height: 1.5rem;
      font-size: var(--text-sm);
    }
    .good {
      color: var(--color-gain);
      font-weight: var(--weight-medium);
    }
    .bad {
      color: var(--color-loss);
      overflow-wrap: anywhere;
    }
    .facts {
      display: grid;
      gap: var(--space-2) var(--space-4);
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 10rem), 1fr));
      margin: 0;
      dt {
        font-size: var(--text-xs);
        color: var(--color-ink-3);
      }
      dd {
        margin: 0;
      }
    }
    .help {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
      p {
        margin: var(--space-2) 0 0;
        overflow-wrap: anywhere;
      }
      summary {
        cursor: pointer;
        min-height: 32px;
        display: flex;
        align-items: center;
        @include bp.coarse {
          min-height: var(--touch-min);
        }
        @include bp.phone {
          min-height: var(--touch-min);
        }
      }
    }
  `,
})
export class FormulaEditor {
  private readonly factors = inject(FactorsService);

  /** The formula to start from. A new value replaces what was typed. */
  readonly initial = input('');
  /** The formula when it checks out, `null` while it does not. */
  readonly valid = output<string | null>();

  protected readonly text = linkedSignal(() => this.initial());
  protected readonly check = signal<ExpressionCheckView | null>(null);
  protected readonly checking = signal(false);
  protected readonly failed = signal(false);
  protected readonly fields = FORMULA_FIELDS.join(', ');
  protected readonly functions = FORMULA_FUNCTIONS.join(', ');
  protected readonly warmup = warmupText;

  private timer: ReturnType<typeof setTimeout> | null = null;
  private seq = 0;

  constructor() {
    inject(DestroyRef).onDestroy(() => this.clearTimer());
    // Check what the page opened with, and again when it hands a new formula.
    effect(() => {
      this.initial();
      untracked(() => this.schedule(0));
    });
  }

  protected edit(value: string): void {
    this.text.set(value);
    this.valid.emit(null);
    this.schedule(CHECK_DELAY_MS);
  }

  private schedule(delay: number): void {
    this.clearTimer();
    const expression = this.text().trim();
    const seq = ++this.seq;
    if (!expression) {
      this.check.set(null);
      this.checking.set(false);
      this.failed.set(false);
      this.valid.emit(null);
      return;
    }
    this.checking.set(true);
    this.timer = setTimeout(() => void this.run(expression, seq), delay);
  }

  private async run(expression: string, seq: number): Promise<void> {
    try {
      const result = await this.factors.check(expression);
      if (seq !== this.seq) return; // typed on since
      this.failed.set(false);
      this.check.set(result);
      this.valid.emit(result.ok ? expression : null);
    } catch {
      if (seq !== this.seq) return;
      this.check.set(null);
      this.failed.set(true);
      this.valid.emit(null);
    } finally {
      if (seq === this.seq) this.checking.set(false);
    }
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}
