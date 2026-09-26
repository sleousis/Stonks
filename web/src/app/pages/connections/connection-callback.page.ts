import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  resource,
} from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { ConnectionsService } from '../../api/connections.service';
import type { ConnectedAccountView, ConnectionView } from '../../api/models';
import { errorMessage } from '../../core/http/api-error';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { AccountList } from './account-list';
import { CONNECTION_STATUS_LABEL, providerName } from './connection-labels';

interface Finished {
  connection: ConnectionView;
  accounts: ConnectedAccountView[];
  provider: string;
}

/** Thrown when the return address lacks what the callback route needs. */
class IncompleteReturn extends Error {
  constructor() {
    super('This return link is missing its details. Start connecting again.');
  }
}

/**
 * Where a provider's hosted sign-in sends the browser back. Passes the
 * query parameters (`connection_id`, `state` and the provider's `status`)
 * to the callback route once, then shows the connection and its accounts.
 */
@Component({
  selector: 'app-connection-callback-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, EmptyState, LoadingState, StatusPill, AccountList, RouterLink],
  template: `
    <app-page-header
      title="Finishing your connection"
      description="Checking with the broker and reading your accounts."
    />
    <section class="panel" aria-labelledby="result-title">
      <div class="panel-head">
        <h2 id="result-title">
          @if (result.hasValue()) {
            {{ result.value().provider }}
          } @else {
            Connection
          }
        </h2>
      </div>
      <div class="panel-body">
        @if (result.error(); as err) {
          <!-- No retry: the provider's state works once, so a new attempt starts over. -->
          <div class="failed" role="alert">
            <p class="title">Could not finish connecting</p>
            <p>{{ message(err) }}</p>
          </div>
          <p class="actions">
            <a class="btn btn-primary" routerLink="/connections">Back to connections</a>
          </p>
        } @else if (!result.hasValue()) {
          <app-loading-state label="Finishing your connection" [rows]="2" />
        } @else {
          @let r = result.value();
          @if (r.connection.status === 'active') {
            <p class="lead">
              <app-status-pill status="active" [label]="statusLabel.active" />
              {{ r.provider }} is connected.
            </p>
          } @else {
            <p class="lead" role="alert">
              <app-status-pill
                [status]="r.connection.status"
                [label]="statusLabel[r.connection.status]"
              />
              {{ r.connection.last_error ?? 'The connection was not finished at the broker.' }}
              Start connecting again from the connections page.
            </p>
          }
          @if (r.accounts.length === 0) {
            <app-empty-state
              title="No accounts found yet"
              message="Accounts appear after the broker confirms the connection."
            />
          } @else {
            <app-account-list [accounts]="r.accounts" [portfolios]="portfolios.options()" />
          }
          <p class="actions">
            <a class="btn btn-primary" [routerLink]="['/connections', r.connection.id]"
              >Open this connection</a
            >
            <a class="btn" routerLink="/connections">Back to connections</a>
          </p>
        }
      </div>
    </section>
  `,
  styles: `
    .failed {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border-left: 3px solid var(--color-loss);
      background: var(--color-loss-soft);
      border-radius: var(--radius-sm);
      overflow-wrap: anywhere;
    }
    .failed p {
      margin: 0;
    }
    .failed .title {
      font-weight: var(--weight-semibold);
      color: var(--color-loss);
    }
    .lead {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      margin: 0 0 var(--space-3);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      margin: var(--space-4) 0 0;
    }
    @media (max-width: 767.98px) {
      .actions .btn {
        width: 100%;
      }
    }
  `,
})
export class ConnectionCallbackPage implements OnInit {
  private readonly api = inject(ConnectionsService);
  private readonly route = inject(ActivatedRoute);
  protected readonly portfolios = inject(PortfolioContextService);
  protected readonly statusLabel = CONNECTION_STATUS_LABEL;
  protected readonly message = errorMessage;

  private readonly query = computed(() => {
    const q = this.route.snapshot.queryParamMap;
    return { connectionId: q.get('connection_id'), state: q.get('state'), status: q.get('status') };
  });

  /** Runs once: the provider's `state` is single-use. */
  protected readonly result = resource({
    loader: async (): Promise<Finished> => {
      const { connectionId, state, status } = this.query();
      if (!connectionId || !state) throw new IncompleteReturn();
      const connection = await this.api.completePortal({
        connection_id: connectionId,
        state,
        ...(status ? { status } : {}),
      });
      const [accounts, providers] = await Promise.all([
        this.api.accounts(connection.id),
        this.api.providers().catch(() => undefined),
      ]);
      return { connection, accounts, provider: providerName(providers, connection.provider) };
    },
  });

  ngOnInit(): void {
    void this.portfolios.load();
  }
}
