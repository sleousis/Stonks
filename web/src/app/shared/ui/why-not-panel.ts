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
  if (d.step === 'risk_rule' && x['original_quantity'] != null) {
    return `${x['original_quantity']} to ${x['adjusted_quantity']} shares`;
  }
  if (d.step === 'buffer' && x['target_weight'] != null) {
    return `target ${x['target_weight']}, now ${x['current_weight'] ?? 'n/a'}`;
  }
  if (d.step === 'rank' && x['winner']) return `${x['winner']} won the book`;
  return '';
}

/**
 * "Why not X": ask why a ticker did or did not trade. On a strategy page it
 * reads that strategy's rows, on a portfolio page the whole book's.
 */
@Component({
  selector: 'app-why-not-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [EmptyState, ErrorState, LoadingState],
  template: `
    <section class="panel" aria-labelledby="why-not-title">
      <div class="panel-head">
        <h2 id="why-not-title">Why not {{ shownTicker() || 'X' }}?</h2>
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
          <ul class="rows">
            @for (d of rows.value().items; track d.tick_id + d.ticker) {
              <li class="row">
                <span class="chip" [attr.data-outcome]="d.outcome">{{ label(d.outcome) }}</span>
                <span class="what">
                  <strong>{{ d.summary }}</strong>
                  <span class="muted">
                    {{ d.as_of }}
                    @if (d.strategy_id) {
                      · {{ d.strategy_id }}
                    }
                    @if (detail(d); as extra) {
                      · {{ extra }}
                    }
                  </span>
                </span>
              </li>
            }
          </ul>
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
  protected readonly shownTicker = computed(() => this.ticker().toUpperCase());

  protected readonly rows = resource({
    params: () => ({
      portfolioId: this.portfolioId() ?? this.ctx.selectedId(),
      strategyId: this.strategyId(),
      ticker: this.ticker(),
    }),
    loader: ({ params }) => this.api.list({ ...params, limit: 20 }),
  });

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

  protected detail(d: TradeDecisionView): string {
    return decisionDetail(d);
  }
}
