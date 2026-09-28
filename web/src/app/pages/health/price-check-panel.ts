import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import { HealthService } from '../../api/health.service';
import type { PriceCheckItemView, PriceCheckView } from '../../api/models';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { autoRefresh } from '../../shared/auto-refresh';
import { ErrorState } from '../../shared/ui/states';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';

export interface PriceCheckState {
  status: string;
  label: string;
  tone: PillTone;
}

/** How a check's status reads. */
export function priceCheckState(c: PriceCheckView): PriceCheckState {
  switch (c.status) {
    case 'clean':
      return { status: 'ok', label: 'Prices agree', tone: 'positive' };
    case 'gaps':
      return { status: 'warn', label: 'Gaps, those buys held', tone: 'warn' };
    case 'systematic':
      return { status: 'halted', label: 'Vendor off, trading halted', tone: 'negative' };
    default:
      return { status: 'skipped', label: 'Nothing compared', tone: 'neutral' };
  }
}

/** The items worth a line: gaps first, then tickers not compared. */
export function notableItems(c: PriceCheckView): PriceCheckItemView[] {
  const rank = (i: PriceCheckItemView) => (i.status === 'gap' ? 0 : 1);
  return c.items
    .filter((i) => i.status !== 'ok')
    .sort((a, b) => rank(a) - rank(b) || a.ticker.localeCompare(b.ticker));
}

/**
 * Health: the newest second-source price check (roadmap 23.6). Before the
 * tick, held and signalled tickers' vendor closes and adjusted returns are
 * compared with a second source. A gap holds that ticker's new buys for the
 * day. When most tickers gap the vendor is off and the operational halt
 * opens. Hidden before the first check.
 */
@Component({
  selector: 'app-price-check-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StatusPill, ErrorState],
  template: `
    @if (check.error(); as err) {
      <section class="panel" aria-labelledby="price-check-title">
        <div class="panel-head"><h2 id="price-check-title">Second-source prices</h2></div>
        <app-error-state
          title="Could not load the price check"
          [error]="err"
          (retry)="check.reload()"
        />
      </section>
    } @else if (check.hasValue() && check.value(); as c) {
      <section class="panel" aria-labelledby="price-check-title">
        <div class="panel-head">
          <h2 id="price-check-title">Second-source prices</h2>
          <app-status-pill
            [status]="state(c).status"
            [label]="state(c).label"
            [tone]="state(c).tone"
          />
        </div>
        <div class="body">
          <p class="muted num">
            {{ c.tickers_compared }} of {{ c.tickers_checked }} tickers compared with
            {{ c.source }} for {{ c.as_of }}, {{ when(c.checked_at) }}.
          </p>
          @if (c.held.length) {
            <p class="problem" role="note">
              New buys held today: <strong>{{ c.held.join(', ') }}</strong
              >. Sells still go out.
            </p>
          }
          @if (c.halt_id) {
            <p class="hint">
              Halt #{{ c.halt_id }} stops new buys everywhere. It clears after a later check agrees
              and the health check runs.
            </p>
          }
          @if (items().length) {
            <ul class="items">
              @for (i of items(); track i.ticker) {
                <li [class.gap]="i.status === 'gap'">
                  <strong class="key">{{ i.ticker }}</strong>
                  <span class="muted">{{ i.detail }}</span>
                </li>
              }
            </ul>
          }
        </div>
      </section>
    }
  `,
  styles: `
    .body {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
    }
    .items {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding-left: var(--space-4);
    }
    .items li {
      overflow-wrap: anywhere;
    }
    .items li.gap strong {
      color: var(--color-loss);
    }
    .key {
      margin-right: var(--space-2);
      font-family: var(--font-mono);
    }
    .problem {
      padding: var(--space-2) var(--space-3);
      border-radius: var(--radius-xs);
      background: var(--color-loss-soft);
      overflow-wrap: anywhere;
    }
  `,
})
export class PriceCheckPanel {
  private readonly health = inject(HealthService);

  protected readonly check = resource({ loader: () => this.health.priceCheck() });
  private readonly auto = autoRefresh(() => [this.check]);

  protected readonly items = computed(() => {
    const c = this.check.hasValue() ? this.check.value() : null;
    return c ? notableItems(c) : [];
  });
  protected readonly state = priceCheckState;
  protected readonly when = (at: string) => `${formatAgo(at)}, ${formatDateTime(at)}`;

  reload(): void {
    this.auto.refresh();
  }
}
