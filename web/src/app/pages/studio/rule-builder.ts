import { ChangeDetectionStrategy, Component, computed, input, model, signal } from '@angular/core';

import { ConditionEditor } from './condition-editor';
import {
  type ConditionNode,
  INTERVALS,
  type IndicatorDef,
  type IndicatorKind,
  type IssueMap,
  type PriceField,
  type RuleSpecDoc,
  changeKind,
  defaultComparison,
  newIndicator,
  renameIndicator,
} from './rule-spec';

const MAX_INDICATORS = 32;

/**
 * The visual rule builder: universe, indicators, entry / exit conditions,
 * ranking, sizing and risk exits. Two-way bound to a RuleSpecDoc; every edit
 * sets a new document. Validation issues arrive keyed by the API's dotted
 * path and render under the field they belong to; each control carries
 * `data-path` so the issue summary can focus it.
 */
@Component({
  selector: 'app-rule-builder',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ConditionEditor],
  templateUrl: './rule-builder.html',
  styleUrl: './rule-builder.scss',
})
export class RuleBuilder {
  readonly spec = model.required<RuleSpecDoc>();
  readonly kinds = input.required<readonly IndicatorKind[]>();
  readonly assetClasses = input.required<readonly string[]>();
  readonly issues = input<IssueMap>({});

  protected readonly intervals = INTERVALS;
  protected readonly newKind = signal('sma');

  protected readonly indicatorIds = computed(() => this.spec().indicators.map((i) => i.id));
  protected readonly canAddIndicator = computed(
    () => this.spec().indicators.length < MAX_INDICATORS,
  );
  protected readonly tickersText = computed(() => (this.spec().universe.tickers ?? []).join(', '));

  protected err(path: string): readonly string[] {
    return this.issues()[path] ?? [];
  }

  protected invalid(path: string): boolean {
    return this.err(path).length > 0;
  }

  protected kindOf(ind: IndicatorDef): IndicatorKind | undefined {
    return this.kinds().find((k) => k.kind === ind.kind);
  }

  private update(fn: (doc: RuleSpecDoc) => RuleSpecDoc): void {
    this.spec.set(fn(this.spec()));
  }

  // ---- basics --------------------------------------------------------------

  protected setText(key: 'name' | 'description' | 'interval', value: string): void {
    this.update((d) => ({ ...d, [key]: value }));
  }

  protected toggleAssetClass(ac: string, on: boolean): void {
    this.update((d) => {
      const current = d.universe.asset_classes.filter((x) => x !== ac);
      const next = on ? [...current, ac] : current;
      const ordered = this.assetClasses().filter((x) => next.includes(x));
      const extra = next.filter((x) => !ordered.includes(x));
      return { ...d, universe: { ...d.universe, asset_classes: [...ordered, ...extra] } };
    });
  }

  protected setTickers(raw: string): void {
    const tickers = raw
      .split(/[\s,;]+/)
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean);
    this.update((d) => {
      // An empty allow-list is left out of the spec (null means "every ticker").
      const rest = without(d.universe, 'tickers');
      const universe = tickers.length ? { ...rest, tickers } : rest;
      return { ...d, universe };
    });
  }

  // ---- indicators ----------------------------------------------------------

  protected addIndicator(): void {
    this.update((d) => ({
      ...d,
      indicators: [
        ...d.indicators,
        newIndicator(
          this.newKind(),
          this.kinds(),
          d.indicators.map((i) => i.id),
        ),
      ],
    }));
  }

  protected removeIndicator(index: number): void {
    this.update((d) => ({ ...d, indicators: d.indicators.filter((_, i) => i !== index) }));
  }

  protected renameIndicator(index: number, id: string): void {
    this.update((d) => renameIndicator(d, index, id.trim()));
  }

  protected setKind(index: number, kindName: string): void {
    this.update((d) => ({
      ...d,
      indicators: d.indicators.map((ind, i) =>
        i === index ? changeKind(ind, kindName, this.kinds()) : ind,
      ),
    }));
  }

  protected setPeriod(index: number, raw: string): void {
    const value = raw.trim() === '' ? null : Number(raw);
    this.patchIndicator(index, { period: value as number });
  }

  protected setSource(index: number, source: string): void {
    this.patchIndicator(index, { source: source as PriceField });
  }

  private patchIndicator(index: number, patch: Partial<IndicatorDef>): void {
    this.update((d) => ({
      ...d,
      indicators: d.indicators.map((ind, i) => (i === index ? { ...ind, ...patch } : ind)),
    }));
  }

  // ---- conditions ----------------------------------------------------------

  protected setEntry(node: ConditionNode): void {
    this.update((d) => ({ ...d, entry: node }));
  }

  protected setExit(node: ConditionNode | null): void {
    this.update((d) => ({ ...d, exit: node }));
  }

  protected toggleExit(on: boolean): void {
    this.setExit(on ? defaultComparison(this.indicatorIds()) : null);
  }

  protected setExitWhenEntryFalse(on: boolean): void {
    this.update((d) => ({ ...d, exit_when_entry_false: on }));
  }

  // ---- rank, sizing, risk --------------------------------------------------

  protected setRank(key: 'by' | 'order', value: string): void {
    this.update((d) => ({ ...d, rank: { ...d.rank, [key]: value } }));
  }

  protected setSizing(key: 'max_positions' | 'top_k', raw: string): void {
    const value = raw.trim() === '' ? null : Number(raw);
    this.update((d) => {
      if (key === 'top_k' && value === null) {
        return { ...d, sizing: without(d.sizing, 'top_k') };
      }
      return { ...d, sizing: { ...d.sizing, [key]: value } } as RuleSpecDoc;
    });
  }

  /** Allocation is entered as a percentage and stored as a fraction. */
  protected setAllocation(raw: string): void {
    const pct = Number(raw);
    this.update((d) => ({
      ...d,
      sizing: { ...d.sizing, allocation: raw.trim() === '' ? 0 : round(pct / 100) },
    }));
  }

  protected toggleRisk(key: 'stop_loss_pct' | 'take_profit_pct', on: boolean): void {
    const fallback = key === 'stop_loss_pct' ? 0.08 : 0.2;
    this.update((d) => ({ ...d, risk: { ...d.risk, [key]: on ? fallback : null } }));
  }

  protected setRisk(key: 'stop_loss_pct' | 'take_profit_pct', input: HTMLInputElement): void {
    const raw = input.value;
    if (raw.trim() === '') {
      // A cleared (or half-typed) box keeps the exit: the checkbox turns it off.
      input.value = this.pct(this.spec().risk[key]);
      return;
    }
    const value = round(Number(raw) / 100);
    this.update((d) => ({ ...d, risk: { ...d.risk, [key]: value } }));
  }

  protected pct(fraction: number | null | undefined): string {
    return fraction === null || fraction === undefined ? '' : String(round(fraction * 100));
  }
}

function without<T extends object, K extends keyof T>(o: T, key: K): Omit<T, K> {
  return Object.fromEntries(Object.entries(o).filter(([k]) => k !== key)) as Omit<T, K>;
}

function round(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}
