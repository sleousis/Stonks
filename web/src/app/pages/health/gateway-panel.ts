import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LiveService } from '../../api/live.service';
import type { GatewayView } from '../../api/models';
import { formatAgo, formatDateTime } from '../../core/format/format';
import { autoRefresh } from '../../shared/auto-refresh';
import { strategyDisplayName } from '../../shared/strategy-names';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill, type PillTone } from '../../shared/ui/status-pill';

/** A real fault the broker health check found, in plain words. */
const FAULT_WORDS: Record<string, string> = {
  login_refused: 'The broker refused the login',
  wrong_account: 'Logged in to the wrong account',
  competing_session: 'Another session is logged in to this account',
};

export interface GatewayState {
  status: string;
  label: string;
  tone: PillTone;
}

/** Connected, down, or not checked yet. */
export function gatewayState(g: GatewayView): GatewayState {
  if (!g.checked) return { status: 'queued', label: 'Not checked yet', tone: 'neutral' };
  if (g.connected) return { status: 'ok', label: 'Connected', tone: 'positive' };
  return { status: 'unhealthy', label: 'Down', tone: 'negative' };
}

export function faultWords(fault: string | null | undefined): string | null {
  if (!fault) return null;
  return FAULT_WORDS[fault] ?? fault.replace(/_/g, ' ');
}

/** "3 hours ago, 27 Sep 14:00" or "Never". */
export function whenText(at: string | null | undefined): string {
  return at ? `${formatAgo(at)}, ${formatDateTime(at)}` : 'Never';
}

/**
 * Health: each IB Gateway the broker health check watches. Connected or
 * down, the last good check, how long it has been down, the fault, and
 * the auto strategies it paused on your portfolios. Resuming auto needs a
 * fresh code, from the strategy's page.
 */
@Component({
  selector: 'app-gateway-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ModeStamp, StatusPill, EmptyState, ErrorState, LoadingState],
  template: `
    <section class="panel" aria-labelledby="gateways-title">
      <div class="panel-head">
        <h2 id="gateways-title">Broker gateways</h2>
        @if (downCount(); as n) {
          <span class="down num">{{ n }} down</span>
        }
      </div>
      @if (gateways.error(); as err) {
        <app-error-state
          title="Could not load the broker gateways"
          [error]="err"
          (retry)="gateways.reload()"
        />
      } @else if (!gateways.hasValue()) {
        <app-loading-state label="Loading broker gateways" [rows]="2" />
      } @else if (gateways.value().gateways.length === 0) {
        <app-empty-state
          icon="plug"
          compact
          title="No broker gateway"
          message="Trading at Interactive Brokers runs through a gateway. None is set up, so every portfolio trades on paper."
        />
      } @else {
        <ul class="gateways">
          @for (g of gateways.value().gateways; track g.gateway) {
            <li class="gateway" [attr.data-state]="state(g).status">
              <div class="gateway-head">
                <h3>{{ g.gateway }}</h3>
                <app-mode-stamp [live]="g.mode === 'live'" />
                <app-status-pill
                  [status]="state(g).status"
                  [label]="state(g).label"
                  [tone]="state(g).tone"
                />
              </div>
              <dl class="facts">
                <div>
                  <dt>Last good check</dt>
                  <dd class="num">{{ when(g.last_ok_at) }}</dd>
                </div>
                <div>
                  <dt>Last check</dt>
                  <dd class="num">{{ when(g.last_check_at) }}</dd>
                </div>
                @if (!g.connected && g.down_since) {
                  <div>
                    <dt>Down since</dt>
                    <dd class="num">{{ when(g.down_since) }}</dd>
                  </div>
                  <div>
                    <dt>Failed checks in a row</dt>
                    <dd class="num">{{ g.consecutive_failures }}</dd>
                  </div>
                }
                <div>
                  <dt>Your portfolios on it</dt>
                  <dd>{{ g.your_portfolios.length ? g.your_portfolios.join(', ') : 'None' }}</dd>
                </div>
              </dl>
              @if (!g.connected && g.checked) {
                <p class="problem callout" data-tone="loss" role="note">
                  @if (fault(g.fault); as f) {
                    <strong>{{ f }}.</strong>
                  }
                  {{ g.detail || 'No detail recorded.' }} No orders go out through it while it is
                  down, and a missed day is never sent late.
                </p>
              }
              @if (g.paused_books.length || g.paused_elsewhere) {
                <div class="paused">
                  <h4>Auto paused</h4>
                  @if (g.paused_at) {
                    <p class="muted">Paused {{ when(g.paused_at) }}.</p>
                  }
                  @if (g.paused_books.length) {
                    <ul>
                      @for (b of g.paused_books; track b.subscription_id) {
                        <li>
                          <a class="cell-link" [routerLink]="['/strategies', b.strategy_id]">{{
                            name(b.strategy_id)
                          }}</a>
                          <span class="muted"> in {{ b.portfolio_name }}</span>
                        </li>
                      }
                    </ul>
                    <p class="hint">
                      Auto stays paused when the gateway comes back. Turn it on again from the
                      strategy's page, with a fresh code.
                    </p>
                  }
                  @if (g.paused_elsewhere) {
                    <p class="muted num">
                      {{ g.paused_elsewhere }} more in other people's portfolios.
                    </p>
                  }
                </div>
              }
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    @use 'breakpoints' as bp;
    .down {
      color: var(--down);
      font-weight: var(--weight-semibold);
    }
    .gateways {
      display: grid;
      gap: var(--space-3);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    /* Each gateway is a row on a hairline; the status pill carries the state. */
    .gateway {
      display: grid;
      gap: var(--space-3);
      min-width: 0;
      padding: var(--space-4) 0;
      border-bottom: 1px solid var(--rule);
    }
    .gateway[data-state='unhealthy'] {
      padding-left: var(--space-4);
      box-shadow: inset 3px 0 0 var(--down);
    }
    .gateway-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-3);
    }
    h3 {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
      overflow-wrap: anywhere;
    }
    h4 {
      font-size: var(--text-sm);
      font-weight: var(--weight-semibold);
      color: var(--ink-2);
    }
    .facts {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
      gap: var(--space-2) var(--space-4);
      margin: 0;
    }
    dt {
      font-size: var(--text-xs);
      color: var(--muted);
    }
    dd {
      margin: 2px 0 0;
      font-weight: var(--weight-medium);
      overflow-wrap: anywhere;
    }
    .problem {
      overflow-wrap: anywhere;
    }
    .paused {
      display: grid;
      gap: var(--space-2);
    }
    .paused ul {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding-left: var(--space-4);
    }
    .paused li {
      min-height: var(--touch-min);
      display: list-item;
      line-height: var(--touch-min);
    }
    @include bp.from-tablet {
      .paused li {
        min-height: 0;
        line-height: inherit;
      }
    }
  `,
})
export class GatewayPanel {
  private readonly live = inject(LiveService);

  protected readonly gateways = resource({ loader: () => this.live.gateways() });
  /** The broker health check runs every few minutes: read along with it. */
  private readonly auto = autoRefresh(() => [this.gateways]);

  protected readonly downCount = computed(() =>
    this.gateways.hasValue()
      ? this.gateways.value().gateways.filter((g) => g.checked && !g.connected).length
      : 0,
  );

  protected readonly state = gatewayState;
  protected readonly fault = faultWords;
  protected readonly when = whenText;
  protected readonly name = (id: string) => strategyDisplayName(id);

  reload(): void {
    this.auto.refresh();
  }
}
