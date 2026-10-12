import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { OptionChainRow, OptionPayoffView, OptionStructureView } from '../../api/models';
import { OptionsService } from '../../api/options.service';
import { formatDate, formatNumber } from '../../core/format/format';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { OptionsBacktest } from './options-backtest';
import {
  type ChainCell,
  type ChainSide,
  type ChainSort,
  chainLayout,
  inTheMoney,
  optionsStrategyName,
  paramLabel,
  pricingModelsText,
  sortChainRows,
  structureLabel,
} from './options-view';
import { SessionService } from '../../core/auth/session.service';
import { DataPlanNote } from '../../shared/ui/data-plan-note';
import { PayoffDiagram } from './payoff-diagram';

const SIDES: SegmentOption<ChainSide>[] = [
  { value: 'both', label: 'Both' },
  { value: 'calls', label: 'Calls' },
  { value: 'puts', label: 'Puts' },
];

/** Payoff inputs: days to expiry and the deltas a structure reads. */
interface PayoffForm {
  structure: string;
  values: Record<string, number | null>;
}

const DEFAULTS: Record<string, number> = {
  dte: 35,
  delta: 0.3,
  long_delta: 0.5,
  short_delta: 0.25,
  wing_delta: 0.05,
};

/**
 * Options research: a stored chain with our implied vol and Greeks, the
 * payoff of a structure picked from it, the options strategies and a
 * backtest runner. Research only, nothing here trades options.
 */
@Component({
  selector: 'app-options-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DataPlanNote,
    PageHeader,
    Segmented,
    HelpTip,
    LoadingState,
    ErrorState,
    EmptyState,
    PayoffDiagram,
    OptionsBacktest,
  ],
  templateUrl: './options.page.html',
  styleUrl: './options.page.scss',
})
export class OptionsPage {
  private readonly api = inject(OptionsService);

  protected readonly sides = SIDES;
  protected readonly paramLabel = paramLabel;
  protected readonly structureLabel = structureLabel;
  protected readonly strategyName = optionsStrategyName;
  protected readonly modelsText = pricingModelsText;
  private readonly session = inject(SessionService);
  protected readonly isAdmin = computed(() => this.session.can('operations.run'));

  protected readonly underlyings = resource({ loader: () => this.api.underlyings() });
  protected readonly strategies = resource({ loader: () => this.api.strategies() });
  protected readonly structures = resource({ loader: () => this.api.structures() });

  protected readonly underlying = signal('');
  protected readonly asOf = signal('');
  protected readonly expiry = signal('');
  protected readonly side = signal<ChainSide>('both');

  protected readonly chain = resource({
    params: () =>
      this.underlying()
        ? { underlying: this.underlying(), asOf: this.asOf(), expiry: this.expiry() }
        : undefined,
    loader: ({ params }) => this.api.chain(params.underlying, params.asOf, params.expiry),
  });

  /** The mirrored chain's columns: calls, the strike, puts. */
  protected readonly layout = computed(() => chainLayout(this.side()));
  /** Columns each side keeps on phones (bid, ask, delta). */
  protected readonly phoneCount = computed(() => this.layout().puts.filter((c) => c.phone).length);
  /** The rows by strike, with the underlying's price marked between two strikes. */
  protected readonly chainRows = computed(() => {
    if (!this.chain.hasValue()) return [];
    const { rows: raw, spot } = this.chain.value();
    const sort = this.chainSort();
    const { calls, puts } = this.layout();
    const rows = sortChainRows(raw, sort, [...calls, ...puts]);
    // The price line sits between two strikes only while rows run by strike, low to high.
    const byStrike = !sort || (sort.key === 'strike' && sort.dir === 'asc');
    let marked = !byStrike || spot === null || spot === undefined;
    return rows.map((row: OptionChainRow) => {
      const spotBefore = !marked && row.strike >= (spot as number);
      if (spotBefore) marked = true;
      return { row, spotBefore };
    });
  });
  protected readonly itm = inTheMoney;

  /** The column the chain is sorted by, or null for the strike order (old sortable table). */
  protected readonly chainSort = signal<ChainSort | null>(null);
  /** Said to screen readers after each sort. */
  protected readonly sortMessage = signal('');

  /** `aria-sort` for a header: the strike counts as sorted while no other column is. */
  protected ariaSort(key: string): 'ascending' | 'descending' | null {
    const sort = this.chainSort() ?? (key === 'strike' ? { key, dir: 'asc' as const } : null);
    if (!sort || sort.key !== key) return null;
    return sort.dir === 'asc' ? 'ascending' : 'descending';
  }

  /** Numbers sort high to low first, the strike low to high; a second press flips it. */
  protected toggleSort(key: string, label: string): void {
    const current = this.chainSort() ?? { key: 'strike', dir: 'asc' as const };
    const first: ChainSort['dir'] = key === 'strike' ? 'asc' : 'desc';
    const dir: ChainSort['dir'] =
      current.key === key ? (current.dir === 'asc' ? 'desc' : 'asc') : first;
    this.chainSort.set({ key, dir });
    this.sortMessage.set(`Sorted by ${label}, ${dir === 'asc' ? 'ascending' : 'descending'}`);
  }

  /** The header's visible words: "bid" under Calls, the short form for IV. */
  protected shortLabel(col: ChainCell): string {
    return col.prefix ? col.label.slice(col.prefix.length + 1) : col.label;
  }

  protected strikeText(strike: number): string {
    return formatNumber(strike, { digits: 2 });
  }

  protected spotText(spot: number | null | undefined): string {
    return spot === null || spot === undefined ? '' : formatNumber(spot, { digits: 2 });
  }

  /** "AAA.US at 97.04. 14 strikes, expiry in 42 days." */
  protected readonly standfirst = computed(() => {
    if (!this.chain.hasValue()) {
      return 'Look at option prices and test option strategies on past prices. Nothing here trades.';
    }
    const c = this.chain.value();
    const price = c.spot === null || c.spot === undefined ? '' : ` at ${this.spotText(c.spot)}`;
    return `${this.underlying()}${price}. ${c.rows.length} strikes, expiry in ${c.days_to_expiry} days. Nothing here trades.`;
  });

  protected readonly payoffForm = signal<PayoffForm>({ structure: '', values: {} });
  protected readonly payoff = signal<OptionPayoffView | null>(null);
  protected readonly payoffError = signal<unknown>(null);
  protected readonly drawing = signal(false);

  protected readonly structure = computed<OptionStructureView | null>(() => {
    if (!this.structures.hasValue()) return null;
    return this.structures.value().find((s) => s.name === this.payoffForm().structure) ?? null;
  });

  constructor() {
    // Open the first stored underlying, and the first structure.
    effect(() => {
      if (this.underlyings.hasValue() && !this.underlying()) {
        const first = this.underlyings.value()[0];
        if (first) this.underlying.set(first.underlying);
      }
    });
    effect(() => {
      if (this.structures.hasValue() && !this.payoffForm().structure) {
        const first = this.structures.value()[0];
        if (first) this.pickStructure(first.name);
      }
    });
  }

  protected pickUnderlying(value: string): void {
    this.underlying.set(value);
    this.expiry.set('');
    this.payoff.set(null);
  }

  protected pickDay(value: string): void {
    this.asOf.set(value);
    this.expiry.set('');
    this.payoff.set(null);
  }

  protected chainTitle(asOf: string, spot: number | null | undefined): string {
    const price = spot === null || spot === undefined ? '' : `, price ${formatNumber(spot)}`;
    return `${this.underlying()} on ${formatDate(asOf)}${price}`;
  }

  protected pickStructure(name: string): void {
    const s = this.structures.hasValue()
      ? this.structures.value().find((x) => x.name === name)
      : undefined;
    const values: Record<string, number | null> = {};
    for (const p of s?.params ?? []) values[p] = DEFAULTS[p] ?? null;
    this.payoffForm.set({ structure: name, values });
    this.payoff.set(null);
    this.payoffError.set(null);
  }

  protected setParam(name: string, raw: string): void {
    const n = raw.trim() === '' ? null : Number(raw);
    this.payoffForm.update((f) => ({
      ...f,
      values: { ...f.values, [name]: n === null || Number.isNaN(n) ? null : n },
    }));
  }

  protected structureList(names: readonly string[]): string {
    return names.map(structureLabel).join(', ');
  }

  protected isDte(name: string): boolean {
    return name === 'dte';
  }

  async drawPayoff(): Promise<void> {
    const f = this.payoffForm();
    if (!f.structure || !this.underlying()) return;
    const values = Object.fromEntries(
      Object.entries(f.values).filter(([, v]) => v !== null && v !== undefined),
    );
    this.drawing.set(true);
    this.payoffError.set(null);
    try {
      const view = await this.api.payoff({
        underlying: this.underlying(),
        as_of: this.asOf() || null,
        structure: f.structure,
        ...values,
      });
      this.payoff.set(view);
    } catch (err) {
      this.payoff.set(null);
      this.payoffError.set(err);
    } finally {
      this.drawing.set(false);
    }
  }
}
