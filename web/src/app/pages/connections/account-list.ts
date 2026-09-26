import { ChangeDetectionStrategy, Component, input, output, signal } from '@angular/core';

import type { ConnectedAccountView } from '../../api/models';
import type { PortfolioRef } from '../../api/portfolios.service';
import { ModeStamp } from '../../shared/ui/mode-stamp';

/** Link this account to that portfolio (null: a new portfolio for it). */
export interface LinkChoice {
  account: ConnectedAccountView;
  portfolioId: string | null;
}

/**
 * The accounts a connection found, with the portfolio each one feeds. With
 * `linkable`, each account gets a portfolio choice and a Link button.
 */
@Component({
  selector: 'app-account-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ModeStamp],
  template: `
    <ul class="accounts">
      @for (a of accounts(); track a.external_account_id) {
        <li class="account">
          <div class="who">
            <span class="name">{{ a.name }}</span>
            <span class="muted">
              {{ a.institution ?? '' }}{{ a.institution && a.number_mask ? ' · ' : ''
              }}{{ a.number_mask ? 'ending ' + a.number_mask : '' }}
              {{ a.currency }}
            </span>
          </div>
          <div class="linked">
            @if (a.portfolio_id) {
              <span>Feeds {{ portfolioName(a.portfolio_id) }}</span>
              @if (portfolio(a.portfolio_id)?.mode; as mode) {
                <app-mode-stamp [live]="mode === 'live'" />
              }
            } @else {
              <span class="muted">Not linked to a portfolio</span>
            }
          </div>
          @if (linkable()) {
            <div class="link">
              <label class="visually-hidden" [for]="'link-' + a.external_account_id"
                >Portfolio for {{ a.name }}</label
              >
              <select
                class="input"
                [id]="'link-' + a.external_account_id"
                [disabled]="!canLink()"
                (change)="choose(a, $any($event.target).value)"
              >
                <option value="" [selected]="!chosen(a)">A new portfolio for this account</option>
                @for (p of portfolios(); track p.id) {
                  <option [value]="p.id" [selected]="chosen(a) === p.id">
                    {{ p.name }}{{ p.mode === 'live' ? ' (live)' : '' }}
                  </option>
                }
              </select>
              <button
                type="button"
                class="btn"
                [disabled]="!canLink() || busyId() === a.external_account_id"
                [attr.aria-busy]="busyId() === a.external_account_id"
                (click)="link.emit({ account: a, portfolioId: chosen(a) })"
              >
                {{ a.portfolio_id ? 'Link again' : 'Link' }}
              </button>
            </div>
          }
        </li>
      }
    </ul>
  `,
  styles: `
    .accounts {
      list-style: none;
      margin: 0;
      padding: 0;
    }
    .account {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) 0;
      border-top: 1px solid var(--color-border);
      min-width: 0;
    }
    .account:first-child {
      border-top: 0;
    }
    .who {
      display: grid;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .name {
      font-weight: var(--weight-semibold);
    }
    .who .muted {
      font-size: var(--text-sm);
    }
    .linked {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      font-size: var(--text-sm);
    }
    .link {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      min-width: 0;
    }
    .link select {
      flex: 1 1 14rem;
      min-width: 0;
      max-width: 24rem;
    }
    @media (max-width: 767.98px) {
      .link select,
      .link .btn {
        flex-basis: 100%;
        max-width: none;
      }
    }
  `,
})
export class AccountList {
  readonly accounts = input.required<readonly ConnectedAccountView[]>();
  readonly portfolios = input<readonly PortfolioRef[]>([]);
  readonly linkable = input(false);
  readonly canLink = input(true);
  readonly busyId = input<string | null>(null);
  readonly link = output<LinkChoice>();

  /** The portfolio picked per account; unset means the listed one it feeds now, else a new one. */
  private readonly picks = signal<Record<string, string | null>>({});

  protected chosen(a: ConnectedAccountView): string | null {
    const picks = this.picks();
    if (a.external_account_id in picks) return picks[a.external_account_id];
    return a.portfolio_id && this.portfolio(a.portfolio_id) ? a.portfolio_id : null;
  }

  protected choose(a: ConnectedAccountView, value: string): void {
    this.picks.update((p) => ({ ...p, [a.external_account_id]: value || null }));
  }

  protected portfolio(id: string): PortfolioRef | undefined {
    return this.portfolios().find((p) => p.id === id);
  }

  protected portfolioName(id: string): string {
    return this.portfolio(id)?.name ?? 'a portfolio';
  }
}
