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

const NOT_LISTED = 'Not in the fund list';

/** Readable labels for the groups the API names in lower case. */
function groupLabel(key: string): string {
  if (key === 'cash') return 'Cash';
  if (key === 'unknown') return 'Unknown';
  if (key === 'not listed') return NOT_LISTED;
  return key;
}

function split(direct: number, viaFunds: number, total: number, funds: readonly string[]): string {
  if (!viaFunds) return '';
  const through = funds.length ? ` through ${funds.join(' and ')}` : ' through funds';
  const share = (n: number) => (total > 0 ? formatPercent(n / total, { digits: 1 }) : '');
  return direct ? `${share(direct)} direct, ${share(viaFunds)}${through}` : `All${through}`;
}

/** Rows for one view of the look-through, largest first as the API sends them. */
export function lookThroughRows(
  lt: LookThrough,
  by: Dimension,
  total: number,
  unlisted: readonly string[] = [],
): Row[] {
  if (by === 'names') {
    const rows: Row[] = lt.names.map((n) => ({
      key: n.key,
      label: n.name ? `${n.name} (${n.key})` : n.key,
      value: n.value,
      weight: n.weight,
      detail: unlisted.includes(n.key)
        ? 'fund, no list'
        : split(n.direct_value, n.fund_value, total, n.funds) || null,
    }));
    // The part of the funds their lists leave out, so the weights add up.
    const rest = lt.fund_value - lt.listed_fund_value;
    if (rest > 0) {
      rows.push({
        key: 'not listed',
        label: NOT_LISTED,
        value: rest,
        weight: total > 0 ? rest / total : null,
        detail: null,
      });
    }
    return rows;
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
  host: { class: 'span-12', '[class.is-hidden]': 'hidden()' },
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
          @for (f of funds(); track f.fund) {
            <p class="hint">
              {{ f.fund }}: {{ share(f.covered) }} of its holdings are listed, as of
              <span class="num">{{ day(f.as_of) }}</span
              >.
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
    :host(.is-hidden) {
      display: none;
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
      font-size: var(--text-sm);
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
      max-width: 70ch;
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
    return lookThroughRows(
      v.look_through,
      this.dimension(),
      v.total_value,
      v.funds_without_list ?? [],
    );
  });
  /** Only worth a panel when a held fund has a published list (or the load failed). */
  protected readonly hidden = computed(
    () => !this.data.error() && (!this.data.hasValue() || this.funds().length === 0),
  );
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
  protected share(value: number): string {
    return formatPercent(value, { digits: 0 });
  }
  protected day(value: string): string {
    return formatDate(value);
  }
}
