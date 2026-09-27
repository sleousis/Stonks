import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { ConnectionsService } from '../../api/connections.service';
import type { SyncResultView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { AgoPipe, DateTimePipe } from '../../shared/format.pipes';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { AccountList, type LinkChoice } from './account-list';
import { CONNECTION_STATUS_LABEL, SYNC_STATUS_LABEL, providerName } from './connection-labels';

/**
 * One broker connection: its accounts and the portfolios they feed, Link,
 * Sync now (with what the sync found) and Disconnect.
 */
@Component({
  selector: 'app-connection-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    PermissionNote,
    EmptyState,
    ErrorState,
    LoadingState,
    StatusPill,
    AccountList,
    RouterLink,
    AgoPipe,
    DateTimePipe,
  ],
  templateUrl: './connection-detail.page.html',
  styleUrl: './connection-detail.page.scss',
})
export class ConnectionDetailPage implements OnInit {
  readonly id = input.required<string>();

  private readonly api = inject(ConnectionsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);
  protected readonly session = inject(SessionService);
  protected readonly portfolios = inject(PortfolioContextService);

  protected readonly providers = resource({ loader: () => this.api.providers() });
  protected readonly connection = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.api.get(params.id),
  });
  protected readonly accounts = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.api.accounts(params.id),
  });

  protected readonly name = computed(() => {
    if (!this.connection.hasValue()) return 'Broker connection';
    const providers = this.providers.hasValue() ? this.providers.value() : undefined;
    return providerName(providers, this.connection.value().provider);
  });

  protected readonly statusLabel = CONNECTION_STATUS_LABEL;
  protected readonly syncLabel = SYNC_STATUS_LABEL;
  protected readonly canLink = computed(() => this.session.can('portfolio.manage'));
  protected readonly canManage = computed(() => this.session.can('connection.manage'));

  protected readonly syncing = signal(false);
  protected readonly syncResult = signal<SyncResultView | null>(null);
  protected readonly linkingId = signal<string | null>(null);
  protected readonly disconnecting = signal(false);

  ngOnInit(): void {
    void this.portfolios.load();
  }

  protected portfolioName(id: string): string {
    return this.portfolios.options().find((p) => p.id === id)?.name ?? 'a portfolio';
  }

  async link(choice: LinkChoice): Promise<void> {
    const target = choice.portfolioId ? this.portfolioName(choice.portfolioId) : 'a new portfolio';
    const ok = await this.confirm.confirm({
      title: `Link ${choice.account.name}?`,
      message: `Each sync copies its positions and cash into ${target}. Nothing is traded.`,
      confirmLabel: 'Link',
    });
    if (!ok) return;
    this.linkingId.set(choice.account.external_account_id);
    try {
      await this.api.link(this.id(), {
        external_account_id: choice.account.external_account_id,
        portfolio_id: choice.portfolioId,
      });
      this.toasts.success(`Linked ${choice.account.name} to ${target}.`);
      this.accounts.reload();
      void this.portfolios.load(true);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.linkingId.set(null);
    }
  }

  async sync(): Promise<void> {
    if (this.syncing()) return;
    const name = this.name();
    const ok = await this.confirm.confirm({
      title: `Sync ${name} now?`,
      message: 'Reads positions, cash and activity from the broker into the linked portfolios.',
      confirmLabel: 'Sync now',
    });
    if (!ok) return;
    this.syncing.set(true);
    try {
      const result = await this.api.sync(this.id());
      this.syncResult.set(result);
      if (result.status === 'error') {
        this.toasts.error(result.error ?? 'The broker did not answer.', `Could not sync ${name}`);
      } else {
        this.toasts.success(`Synced ${name}.`);
      }
      this.connection.reload();
      this.accounts.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.syncing.set(false);
    }
  }

  /**
   * Disconnecting archives the linked portfolios, so it reads as a ticket:
   * each linked account and the portfolio it feeds, LIVE when any of them
   * trades real money, and the provider's name typed to confirm.
   */
  private disconnectTicket(name: string): ConfirmOptions {
    const accounts = this.accounts.hasValue() ? this.accounts.value() : [];
    const linked = accounts.filter((a) => !!a.portfolio_id);
    const books = linked.map((a) => ({
      account: a.name,
      portfolio: this.portfolios.options().find((p) => p.id === a.portfolio_id),
    }));
    const live = books.some((b) => b.portfolio?.trading === 'live');
    const names = [...new Set(books.map((b) => b.portfolio?.name ?? 'a portfolio'))];
    const archived = names.length
      ? `It archives ${names.length === 1 ? 'the portfolio' : 'the portfolios'} ${names.join(', ')}.`
      : 'No portfolio is linked, so none is archived.';
    return {
      title: `Disconnect ${name}?`,
      message: `Stonks stops reading from ${name}. ${archived} Your account at ${name} is not touched. You may be asked for a fresh code.`,
      confirmLabel: `Disconnect ${name}`,
      tone: 'danger',
      typedConfirmation: name,
      ticket: {
        live,
        lines: books.length
          ? books.map((b) => ({
              label: b.account,
              value: b.portfolio
                ? `${b.portfolio.name} (${b.portfolio.trading === 'live' ? 'live' : 'paper'})`
                : 'A portfolio',
            }))
          : [{ label: 'Linked portfolios', value: 'None' }],
      },
    };
  }

  async disconnect(): Promise<void> {
    if (this.disconnecting()) return;
    const name = this.name();
    const ok = await this.confirm.confirm(this.disconnectTicket(name));
    if (!ok) return;
    this.disconnecting.set(true);
    try {
      // A stale second factor comes back as 403 step_up_required: the session
      // interceptor prompts for a code and retries once.
      const result = await this.api.remove(this.id());
      this.toasts.success(`Disconnected ${name}.`);
      if (result.remote_removed === false) {
        this.toasts.info(
          `${name} did not confirm the removal on its side. You can revoke access in your ${name} account.`,
        );
      }
      await this.router.navigate(['/connections']);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.disconnecting.set(false);
    }
  }
}
