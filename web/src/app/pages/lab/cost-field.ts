import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { CostModelPreset } from '../../api/models';
import { type CostForm, type FormErrors, costHint } from './lab-requests';

/** Cost presets in words, never their ids (NB-07). */
const PRESET_WORDS: Readonly<Record<string, string>> = {
  zero: 'No costs',
  realistic: 'Typical',
  ibkr_tiered: 'IBKR tiered fees',
  ibkr_fixed: 'IBKR fixed fees',
};

export function presetWord(name: string): string {
  return PRESET_WORDS[name] ?? name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

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
            {{ presetWord(m.name) }}
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
  protected readonly presetWord = presetWord;
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
