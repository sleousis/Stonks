import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';

import { DecisionsService } from '../../api/decisions.service';
import type { TradeDecisionView } from '../../api/models';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { strategyDisplayName } from '../strategy-names';
import { EmptyState, ErrorState, LoadingState } from './states';

/** Short labels for the outcome chip. */
export const OUTCOME_LABELS: Record<string, string> = {
  traded: 'Traded',
  trimmed: 'Trimmed',
  kept_out: 'Kept out',
  held: 'Held',
};

/** The rule and quantities, or the weights, as one short line. */
export function decisionDetail(d: TradeDecisionView): string {
  const x = d.detail ?? {};
  if ((d.step === 'risk_rule' || d.step === 'lots') && x['original_quantity'] != null) {
    return `${x['original_quantity']} to ${x['adjusted_quantity']} shares`;
  }
  if (d.step === 'buffer' && x['target_weight'] != null) {
    const now = x['current_weight'];
    return `target ${pct(x['target_weight'])}, now ${now == null ? 'n/a' : pct(now)}`;
  }
  if (d.step === 'rank' && x['winner']) return `${x['winner']} won the book`;
  return '';
}

function pct(v: unknown): string {
  return `${Math.round(Number(v) * 1000) / 10}%`;
}

/** One trading day of decisions. */
export interface DecisionDay {
  day: string;
  rows: TradeDecisionView[];
}

/**
 * The rows to show, grouped by day (newest first). By default only the
 * latest run, without the plain holds that make up most rows.
 */
export function groupDecisions(items: TradeDecisionView[], showAll: boolean): DecisionDay[] {
  const latest = items[0]?.tick_id;
  const shown = showAll ? items : items.filter((d) => d.tick_id === latest && d.outcome !== 'held');
  const days: DecisionDay[] = [];
  for (const d of shown) {
    const last = days.at(-1);
    if (last?.day === d.as_of) last.rows.push(d);
    else days.push({ day: d.as_of, rows: [d] });
  }
  return days;
}

/**
 * Why did a ticker trade or not? Ask about one ticker, or read the latest run. On a strategy page it
 * reads that strategy's rows, on a portfolio page the whole book's.
 */
@Component({
  selector: 'app-why-not-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EmptyState, ErrorState, LoadingState],
  template: `
    <section class="panel" aria-labelledby="why-not-title">
      <div class="panel-head">
        <h2 id="why-not-title">
          {{ strategyId() ? 'Why did it trade a ticker or not?' : 'Why did a ticker trade or not?' }}
        </h2>
      </div>
      <div class="panel-body">
        <form class="ask" (submit)="$event.preventDefault(); ask()" role="search">
          <label for="why-not-ticker" class="visually-hidden">Ticker</label>
          <input
            id="why-not-ticker"
            class="input"
            placeholder="Ticker, e.g. AAPL.US"
            autocomplete="off"
            [value]="draft()"
            (input)="draft.set($any($event.target).value)"
          />
          <button type="submit" class="btn">Ask</button>
          @if (ticker()) {
            <button type="button" class="btn btn-ghost" (click)="clear()">All</button>
          }
        </form>
        @if (rows.error(); as err) {
          <app-error-state
            title="Could not load the decisions"
            [error]="err"
            (retry)="rows.reload()"
          />
        } @else if (!rows.hasValue()) {
          <app-loading-state label="Loading the decisions" [rows]="3" />
        } @else if (rows.value().items.length === 0) {
          <app-empty-state
            title="Nothing recorded yet"
            message="Each trading run records why a ticker did or did not trade."
          />
        } @else {
          @for (g of groups(); track g.day) {
            <h3 class="day">{{ g.day }}</h3>
            <ul class="rows">
              @for (d of g.rows; track d.tick_id + d.ticker) {
                <li class="row">
                  <span class="chip" [attr.data-outcome]="d.outcome">{{ label(d.outcome) }}</span>
                  <span class="what">
                    <strong>{{ d.summary }}</strong>
                    @if (d.strategy_id || detail(d)) {
                      <span class="muted">
                        @if (d.strategy_id) {
                          {{ strategyName(d.strategy_id) }}
                        }
                        @if (detail(d); as extra) {
                          @if (d.strategy_id) {
                            ·
                          }
                          {{ extra }}
                        }
                      </span>
                    }
                  </span>
                </li>
              }
            </ul>
          } @empty {
            <p class="muted">Nothing traded or was left out on the latest run.</p>
          }
          @if (!ticker()) {
            <button type="button" class="btn btn-ghost toggle" (click)="showAll.set(!showAll())">
              {{ showAll() ? 'Show the latest run only' : 'Show all' }}
            </button>
          }
        }
      </div>
    </section>
  `,
  styles: `
    :host {
      display: block;
    }
    .ask {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      margin-bottom: var(--space-3);
    }
    .ask .input {
      flex: 1;
      min-width: 0;
      min-height: var(--touch-min);
    }
    .ask .btn {
      min-height: var(--touch-min);
    }
    .rows {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .row {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
    }
    .what {
      display: grid;
      gap: 2px;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .chip {
      flex: none;
      padding: 2px var(--space-2);
      border-radius: var(--radius-sm);
      background: var(--color-info-soft);
      font-size: var(--text-sm);
    }
    .day {
      margin: var(--space-3) 0 var(--space-2);
      font-size: var(--text-sm);
      color: var(--color-ink-3);
    }
    .day:first-of-type {
      margin-top: 0;
    }
    .toggle {
      margin-top: var(--space-3);
      min-height: var(--touch-min);
    }
    .chip[data-outcome='kept_out'] {
      background: var(--color-warn-soft);
    }
  `,
})
export class WhyNotPanel {
  /** A strategy page: that strategy's rows. */
  readonly strategyId = input<string | null>(null);
  /** A portfolio page: that portfolio (default: the one picked in the header). */
  readonly portfolioId = input<string | null>(null);

  private readonly api = inject(DecisionsService);
  private readonly ctx = inject(PortfolioContextService);

  protected readonly draft = signal('');
  protected readonly ticker = signal('');
  protected readonly showAll = signal(false);

  protected readonly rows = resource({
    params: () => ({
      portfolioId: this.portfolioId() ?? this.ctx.selectedId(),
      strategyId: this.strategyId(),
      ticker: this.ticker(),
    }),
    loader: ({ params }) => this.api.list({ ...params, limit: 50 }),
  });

  /** An asked ticker shows all its rows, otherwise the latest run's trades and cuts. */
  protected readonly groups = computed(() =>
    this.rows.hasValue()
      ? groupDecisions(this.rows.value().items, this.showAll() || !!this.ticker())
      : [],
  );

  protected ask(): void {
    this.ticker.set(this.draft().trim());
  }

  protected clear(): void {
    this.draft.set('');
    this.ticker.set('');
  }

  protected label(outcome: string): string {
    return OUTCOME_LABELS[outcome] ?? outcome;
  }

  protected readonly strategyName = (id: string) => strategyDisplayName(id);

  protected detail(d: TradeDecisionView): string {
    return decisionDetail(d);
  }
}
