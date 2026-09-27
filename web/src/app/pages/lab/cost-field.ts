import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { CostModelPreset } from '../../api/models';
import { type CostForm, type FormErrors, costHint } from './lab-requests';

/**
 * The cost model of a backtest: the configured costs (the default), a named
 * preset, or a flat slippage and fee. Shared by the Lab backtest form and the
 * Studio test tab, so both start from realistic costs.
 */
@Component({
  selector: 'app-cost-field',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="field span-2">
      <label [for]="idPrefix() + '-cost'">Cost model</label>
      <select
        class="input"
        [id]="idPrefix() + '-cost'"
        [attr.aria-describedby]="idPrefix() + '-cost-hint'"
        (change)="patch.emit({ cost: $any($event.target).value })"
      >
        <option value="configured" [selected]="value().cost === 'configured'">Default costs</option>
        @for (m of costModels(); track m.name) {
          <option [value]="m.name" [selected]="value().cost === m.name">
            Preset: {{ m.name }}
          </option>
        }
        <option value="flat" [selected]="value().cost === 'flat'">Flat slippage and fee</option>
      </select>
      <span class="hint" [id]="idPrefix() + '-cost-hint'">{{ hint() }}</span>
    </div>
    @if (value().cost === 'flat') {
      <div class="field">
        <label [for]="idPrefix() + '-slippage'">Slippage (bps)</label>
        <input
          class="input num"
          type="number"
          inputmode="decimal"
          min="0"
          step="any"
          [id]="idPrefix() + '-slippage'"
          [value]="value().slippageBps ?? ''"
          [attr.aria-invalid]="!!errors()['slippageBps']"
          (input)="patch.emit({ slippageBps: num($any($event.target).value) })"
        />
        @if (errors()['slippageBps']; as e) {
          <span class="error">{{ e }}</span>
        }
      </div>
      <div class="field">
        <label [for]="idPrefix() + '-fee'">Fee per trade</label>
        <input
          class="input num"
          type="number"
          inputmode="decimal"
          min="0"
          step="any"
          [id]="idPrefix() + '-fee'"
          [value]="value().feePerTrade ?? ''"
          [attr.aria-invalid]="!!errors()['feePerTrade']"
          (input)="patch.emit({ feePerTrade: num($any($event.target).value) })"
        />
        @if (errors()['feePerTrade']; as e) {
          <span class="error">{{ e }}</span>
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
export class CostField {
  readonly idPrefix = input.required<string>();
  readonly value = input.required<CostForm>();
  readonly costModels = input<readonly CostModelPreset[]>([]);
  readonly errors = input<FormErrors>({});
  /** An extra sentence after the choice's own hint. */
  readonly note = input('');
  readonly patch = output<Partial<CostForm>>();

  protected readonly hint = computed(() =>
    [costHint(this.value().cost, this.costModels()), this.note()].filter(Boolean).join(' '),
  );

  protected num(raw: string): number | null {
    const n = raw.trim() === '' ? null : Number(raw);
    return n === null || Number.isNaN(n) ? null : n;
  }
}
