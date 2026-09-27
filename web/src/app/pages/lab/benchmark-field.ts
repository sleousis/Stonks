import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { HelpTip } from '../../shared/ui/help-tip';
import type { BenchmarkChoice, BenchmarkForm } from './lab-requests';

const CHOICES: readonly { value: BenchmarkChoice; label: string }[] = [
  { value: 'default', label: 'Default' },
  { value: 'auto', label: 'Auto: SPY.US, or the equal-weight universe' },
  { value: 'EW', label: 'Equal-weight universe' },
  { value: 'ticker', label: 'A ticker…' },
  { value: 'none', label: 'None' },
];

/** What the run is compared with; shared by both lab forms. */
@Component({
  selector: 'app-benchmark-field',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HelpTip],
  template: `
    <div class="field">
      <label [for]="idPrefix() + '-bench'">Benchmark <app-help-tip term="benchmark" /></label>
      <select
        class="input"
        [id]="idPrefix() + '-bench'"
        [attr.aria-describedby]="idPrefix() + '-bench-hint'"
        (change)="patch.emit({ benchmark: $any($event.target).value })"
      >
        @for (c of choices; track c.value) {
          <option [value]="c.value" [selected]="value().benchmark === c.value">
            {{ c.label }}
          </option>
        }
      </select>
      <span class="hint" [id]="idPrefix() + '-bench-hint'">
        Results show beta, alpha and excess return against it, on one chart.
      </span>
    </div>
    @if (value().benchmark === 'ticker') {
      <div class="field">
        <label [for]="idPrefix() + '-bench-ticker'">Benchmark ticker</label>
        <input
          class="input"
          autocapitalize="characters"
          spellcheck="false"
          placeholder="QQQ.US"
          [id]="idPrefix() + '-bench-ticker'"
          [attr.aria-invalid]="!!error()"
          [attr.aria-describedby]="error() ? idPrefix() + '-bench-ticker-error' : null"
          [value]="value().benchmarkTicker"
          (input)="patch.emit({ benchmarkTicker: $any($event.target).value })"
        />
        @if (error(); as e) {
          <span class="error" [id]="idPrefix() + '-bench-ticker-error'">{{ e }}</span>
        }
      </div>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class BenchmarkField {
  readonly idPrefix = input.required<string>();
  readonly value = input.required<BenchmarkForm>();
  readonly error = input<string | null>(null);
  readonly patch = output<Partial<BenchmarkForm>>();
  protected readonly choices = CHOICES;
}
