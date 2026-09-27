import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';

import type { MetricView } from '../../api/models';
import { type FilterRow, MAX_FILTERS, filterRow } from './screen-form';

/**
 * The metric filter builder: one row per bound (a metric, at least, at
 * most), grouped by price and fundamentals, with Add and Remove. The
 * screener and the rule universe form both use it, so a rule reads like a
 * screen. Percent metrics are typed in percent; `screen-form.ts` turns the
 * rows into a spec.
 */
@Component({
  selector: 'app-screen-filters',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (filters().length === 0) {
      <p class="hint">No filters yet. Add one to keep only what passes it.</p>
    }
    <ul class="filters" aria-label="Metric filters">
      @for (f of filters(); track f.key; let i = $index) {
        <li class="filter">
          <div class="field metric">
            <label [for]="idPrefix() + '-f-metric-' + f.key">Metric {{ i + 1 }}</label>
            <select
              class="input"
              [id]="idPrefix() + '-f-metric-' + f.key"
              [attr.aria-describedby]="idPrefix() + '-f-hint-' + f.key"
              (change)="edit(f.key, { metric: $any($event.target).value })"
            >
              <option value="" [selected]="!f.metric">Pick a metric</option>
              <optgroup label="Price">
                @for (m of priceMetrics(); track m.id) {
                  <option [value]="m.id" [selected]="f.metric === m.id">{{ m.label }}</option>
                }
              </optgroup>
              <optgroup label="Fundamentals">
                @for (m of fundamentalMetrics(); track m.id) {
                  <option [value]="m.id" [selected]="f.metric === m.id">{{ m.label }}</option>
                }
              </optgroup>
            </select>
            <span class="hint" [id]="idPrefix() + '-f-hint-' + f.key"
              >{{ metric(f.metric)?.description }} {{ unitHint(f.metric) }}</span
            >
          </div>
          <div class="field bound">
            <label [for]="idPrefix() + '-f-min-' + f.key">At least</label>
            <input
              class="input num"
              type="number"
              inputmode="decimal"
              step="any"
              [id]="idPrefix() + '-f-min-' + f.key"
              [value]="f.min"
              (input)="edit(f.key, { min: $any($event.target).value })"
            />
          </div>
          <div class="field bound">
            <label [for]="idPrefix() + '-f-max-' + f.key">At most</label>
            <input
              class="input num"
              type="number"
              inputmode="decimal"
              step="any"
              [id]="idPrefix() + '-f-max-' + f.key"
              [value]="f.max"
              (input)="edit(f.key, { max: $any($event.target).value })"
            />
          </div>
          <button
            type="button"
            class="btn btn-ghost remove"
            [attr.aria-label]="'Remove filter ' + (i + 1)"
            (click)="remove(f.key)"
          >
            Remove
          </button>
        </li>
      }
    </ul>
    <div>
      <button type="button" class="btn" [disabled]="filters().length >= maxFilters" (click)="add()">
        Add a filter
      </button>
    </div>
  `,
  styles: `
    @use 'breakpoints' as bp;

    :host {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
    }

    .hint {
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }

    .filters {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .filter {
      display: grid;
      gap: var(--space-2) var(--space-3);
      padding: var(--space-3);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-md);
      background: var(--color-surface-2);
      grid-template-columns: repeat(2, minmax(0, 1fr));

      .metric {
        grid-column: 1 / -1;
      }

      .remove {
        grid-column: 1 / -1;
        justify-self: start;
      }

      @include bp.from-tablet {
        grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr) auto;
        align-items: start;

        .metric {
          grid-column: auto;
        }

        .remove {
          grid-column: auto;
          margin-top: 1.6rem;
        }
      }
    }
  `,
})
export class ScreenFilters {
  /** The metrics to pick from (`GET /api/screener/metrics`). */
  readonly metrics = input<readonly MetricView[]>([]);
  /** Prefix for element ids, so two builders can share a page. */
  readonly idPrefix = input('sc');
  readonly filters = model<FilterRow[]>([]);

  protected readonly maxFilters = MAX_FILTERS;
  protected readonly priceMetrics = computed(() =>
    this.metrics().filter((m) => m.group === 'price'),
  );
  protected readonly fundamentalMetrics = computed(() =>
    this.metrics().filter((m) => m.group === 'fundamental'),
  );

  protected add(): void {
    this.filters.set([...this.filters(), filterRow()]);
  }

  protected edit(key: number, change: Partial<FilterRow>): void {
    this.filters.set(this.filters().map((f) => (f.key === key ? { ...f, ...change } : f)));
  }

  protected remove(key: number): void {
    this.filters.set(this.filters().filter((f) => f.key !== key));
  }

  protected metric(id: string): MetricView | undefined {
    return this.metrics().find((m) => m.id === id);
  }

  protected unitHint(id: string): string {
    const unit = this.metric(id)?.unit;
    if (unit === 'percent') return 'In percent: 8 means 8%.';
    if (unit === 'money') return 'In the instrument’s currency.';
    return '';
  }
}
