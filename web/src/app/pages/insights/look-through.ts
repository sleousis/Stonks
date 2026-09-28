import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';

import { InsightsService } from '../../api/insights.service';
import type { LookThroughView } from '../../api/models';
import { formatDate, formatMoney, formatPercent } from '../../core/format/format';
import { Segmented, type SegmentOption } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

type LookThrough = LookThroughView['look_through'];
type Dimension = 'names' | 'sector' | 'country';

interface Row {
  key: string;
  label: string;
  value: number;
  weight: number | null | undefined;
  /** "7.0% direct, 2.3% through SPY.US and QQQ.US" */
  detail: string | null;
}

const DIMENSIONS: readonly SegmentOption<Dimension>[] = [
  { value: 'names', label: 'Single names' },
  { value: 'sector', label: 'Sector' },
  { value: 'country', label: 'Country' },
];

/** Readable labels for the groups the API names in lower case. */
function groupLabel(key: string): string {
  if (key === 'cash') return 'Cash';
  if (key === 'unknown') return 'Unknown';
  if (key === 'not listed') return 'Not in the fund lists';
  return key;
}

function split(direct: number, viaFunds: number, total: number, funds: readonly string[]): string {
  if (!viaFunds) return '';
  const through = funds.length ? ` through ${funds.join(' and ')}` : ' through funds';
  const share = (n: number) => (total > 0 ? formatPercent(n / total, { digits: 1 }) : '');
  return direct ? `${share(direct)} direct, ${share(viaFunds)}${through}` : `All${through}`;
}

/** Rows for one view of the look-through, largest first as the API sends them. */
export function lookThroughRows(lt: LookThrough, by: Dimension, total: number): Row[] {
  if (by === 'names') {
    return lt.names.map((n) => ({
      key: n.key,
      label: n.name ? `${n.name} (${n.key})` : n.key,
      value: n.value,
      weight: n.weight,
      detail: split(n.direct_value, n.fund_value, total, n.funds) || null,
    }));
  }
  return lt[by].map((s) => ({
    key: s.key,
    label: groupLabel(s.key),
    value: s.value,
    weight: s.weight,
    detail: split(s.direct_value, s.fund_value, total, []) || null,
  }));
}

/**
 * Look-through exposure (roadmap 23.14): each fund you hold split into the
 * companies it owns, so your real weight in a name, a sector or a country
 * shows. Apple counts AAPL plus its share of SPY and QQQ.
 */
@Component({
  selector: 'app-look-through',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Segmented, LoadingState, ErrorState, EmptyState],
  host: { class: 'span-12' },
  template: `
    <section class="panel" aria-labelledby="look-title">
      <div class="panel-head">
        <h2 id="look-title">Inside your funds</h2>
      </div>
      <div class="panel-body body">
        <p class="hint">
          Each fund you hold counts as the companies it owns, by its latest published weights.
        </p>
        <app-segmented label="Show" [options]="dimensions" [(value)]="dimension" />
        @if (data.error(); as err) {
          <app-error-state
            title="Could not look inside your funds"
            [error]="err"
            (retry)="data.reload()"
          />
        } @else if (!data.hasValue()) {
          <app-loading-state label="Loading the look-through" [rows]="4" />
        } @else if (rows().length === 0) {
          <app-empty-state
            title="Nothing to show"
            message="The book holds no priced positions yet."
          />
        } @else {
          <ul class="rows" [attr.aria-label]="listLabel()">
            @for (r of rows(); track r.key) {
              <li>
                <span class="key">{{ r.label }}</span>
                <span class="num">{{ money(r.value) }}</span>
                <span class="num weight">{{ pct(r.weight) }}</span>
                @if (r.detail) {
                  <span class="detail">{{ r.detail }}</span>
                }
              </li>
            }
          </ul>
          @if (funds().length) {
            <p class="hint">
              Fund lists:
              @for (f of funds(); track f.fund; let last = $last) {
                {{ f.fund }} as of {{ day(f.as_of) }} ({{ pct(f.covered) }} listed){{
                  last ? '.' : ','
                }}
              }
            </p>
          }
          @for (note of notes(); track note) {
            <p class="hint">{{ note }}</p>
          }
        }
      </div>
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .body {
      display: grid;
      gap: var(--space-3);
    }
    .rows {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .rows li {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: var(--space-1) var(--space-3);
      align-items: baseline;
      padding: var(--space-2) 0;
      border-top: 1px solid var(--color-border);
    }
    .key {
      overflow-wrap: anywhere;
    }
    .weight {
      min-width: 5ch;
      text-align: end;
    }
    .detail {
      grid-column: 1 / -1;
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
    .hint {
      margin: 0;
      font-size: var(--text-xs);
      color: var(--color-ink-3);
    }
  `,
})
export class LookThroughPanel {
  private readonly api = inject(InsightsService);
  /** The picked portfolio; the panel waits while it is unknown. */
  readonly portfolio = input<string | null | undefined>(undefined);

  protected readonly dimensions = DIMENSIONS;
  protected readonly dimension = signal<Dimension>('names');
  protected readonly data = resource({
    params: () => (this.portfolio() ? { portfolio: this.portfolio() } : undefined),
    loader: () => this.api.lookThrough(),
  });

  protected readonly rows = computed(() => {
    if (!this.data.hasValue()) return [];
    const v = this.data.value();
    return lookThroughRows(v.look_through, this.dimension(), v.total_value);
  });
  protected readonly funds = computed(() =>
    this.data.hasValue() ? this.data.value().look_through.funds : [],
  );
  protected readonly notes = computed(() => (this.data.hasValue() ? this.data.value().notes : []));
  protected readonly listLabel = computed(
    () => DIMENSIONS.find((d) => d.value === this.dimension())?.label ?? 'Look-through',
  );

  protected money(value: number): string {
    return formatMoney(value, {
      currency: this.data.hasValue() ? this.data.value().currency : null,
    });
  }
  protected pct(value: number | null | undefined): string {
    return formatPercent(value, { digits: 1 });
  }
  protected day(value: string): string {
    return formatDate(value);
  }
}
