import { ChangeDetectionStrategy, Component, computed, input, model, signal } from '@angular/core';

import type { ParameterInfo } from '../../../api/models';
import {
  type ParamField,
  type ParamValue,
  type ParamValues,
  paramError,
  paramFields,
  rangeText,
} from './param-spec';

/**
 * Form fields generated from a strategy's parameter space (catalog
 * `ParameterInfo[]`): number inputs with bounds for int/float, a select for
 * categoricals with choices, a checkbox for bools, text otherwise.
 *
 *   <app-param-form idPrefix="bt" [params]="cls.parameters" [(values)]="params" [showErrors]="tried()" />
 *
 * The parent owns the values (two-way `values`) and checks them with
 * `paramErrors()` before submitting. Errors show once a field is touched, or
 * everywhere when `showErrors` is set.
 */
@Component({
  selector: 'app-param-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (fields().length === 0) {
      <p class="muted none">This strategy has no parameters.</p>
    } @else {
      <div class="grid">
        @for (f of fields(); track f.name) {
          @let id = idPrefix() + '-' + f.name;
          @let err = errorFor(f);
          @switch (f.control) {
            @case ('bool') {
              <div class="field bool">
                <label class="check">
                  <input
                    type="checkbox"
                    [id]="id"
                    [checked]="values()[f.name] === true"
                    [attr.aria-describedby]="f.description ? id + '-hint' : null"
                    (change)="set(f.name, $any($event.target).checked)"
                  />
                  {{ f.label }}
                </label>
                @if (f.description) {
                  <span class="hint" [id]="id + '-hint'">{{ f.description }}</span>
                }
              </div>
            }
            @case ('choice') {
              <div class="field">
                <label [for]="id">{{ f.label }}</label>
                <select
                  class="input"
                  [id]="id"
                  [attr.aria-invalid]="!!err"
                  [attr.aria-describedby]="id + '-hint'"
                  (change)="setChoice(f, $any($event.target).value)"
                >
                  @for (c of f.choices; track $index) {
                    <option [value]="$index" [selected]="values()[f.name] === c">{{ c }}</option>
                  }
                </select>
                <span class="hint" [id]="id + '-hint'">{{ hint(f) }}</span>
              </div>
            }
            @case ('text') {
              <div class="field">
                <label [for]="id">{{ f.label }}</label>
                <input
                  class="input"
                  type="text"
                  autocomplete="off"
                  spellcheck="false"
                  [id]="id"
                  [value]="values()[f.name] ?? ''"
                  [attr.aria-invalid]="!!err"
                  [attr.aria-describedby]="id + '-hint'"
                  (input)="set(f.name, $any($event.target).value)"
                  (blur)="touch(f.name)"
                />
                @if (err) {
                  <span class="error" [id]="id + '-hint'">{{ err }}</span>
                } @else {
                  <span class="hint" [id]="id + '-hint'">{{ hint(f) }}</span>
                }
              </div>
            }
            @default {
              @if (f.control === 'int' || f.control === 'float') {
                <div class="field">
                  <label [for]="id">{{ f.label }}</label>
                  <input
                    class="input num"
                    type="number"
                    [attr.inputmode]="f.control === 'int' ? 'numeric' : 'decimal'"
                    [id]="id"
                    [attr.min]="f.min"
                    [attr.max]="f.max"
                    [attr.step]="f.step"
                    [value]="values()[f.name] ?? ''"
                    [attr.aria-invalid]="!!err"
                    [attr.aria-describedby]="id + '-hint'"
                    (input)="setNumber(f.name, $any($event.target).value)"
                    (blur)="touch(f.name)"
                  />
                  @if (err) {
                    <span class="error" [id]="id + '-hint'">{{ err }}</span>
                  } @else {
                    <span class="hint" [id]="id + '-hint'">{{ hint(f) }}</span>
                  }
                </div>
              }
            }
          }
        }
      </div>
    }
  `,
  styles: `
    :host {
      display: block;
    }
    .grid {
      display: grid;
      gap: var(--space-3) var(--space-4);
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 12rem), 1fr));
    }
    .field {
      align-content: start;
    }
    .none {
      font-size: var(--text-sm);
    }
    .hint,
    .error {
      overflow-wrap: anywhere;
    }
  `,
})
export class ParamForm {
  readonly params = input.required<readonly ParameterInfo[]>();
  readonly values = model.required<ParamValues>();
  /** Prefix for element ids, unique per page (two forms may share a page). */
  readonly idPrefix = input('param');
  /** Show every field's error, e.g. after a submit attempt. */
  readonly showErrors = input(false);

  protected readonly fields = computed(() => paramFields(this.params()));
  private readonly touched = signal<ReadonlySet<string>>(new Set());

  protected errorFor(f: ParamField): string | null {
    if (!this.showErrors() && !this.touched().has(f.name)) return null;
    return paramError(f, this.values()[f.name]);
  }

  protected hint(f: ParamField): string {
    const parts = [f.description, rangeText(f), f.tunable ? null : 'fixed, not tuned'];
    return parts.filter(Boolean).join(' · ') || ' ';
  }

  protected set(name: string, value: ParamValue): void {
    this.values.update((v) => ({ ...v, [name]: value }));
  }

  protected setNumber(name: string, raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.set(name, n === null || Number.isNaN(n) ? null : n);
  }

  protected setChoice(f: ParamField, index: string): void {
    if (f.control !== 'choice') return;
    this.set(f.name, f.choices[Number(index)] ?? null);
  }

  protected touch(name: string): void {
    this.touched.update((s) => new Set(s).add(name));
  }
}
