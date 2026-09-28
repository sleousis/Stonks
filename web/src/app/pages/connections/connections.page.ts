import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { ConnectionsService } from '../../api/connections.service';
import type { ConnectionView, ProviderView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { AgoPipe } from '../../shared/format.pipes';
import { STAGES, STAGE_WORDS } from '../../shared/live-stages';
import { HelpTip } from '../../shared/ui/help-tip';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { BROWSER_REDIRECT } from './browser-redirect';
import {
  CONNECTION_STATUS_LABEL,
  fieldLabel,
  offersPaper,
  providerFlow,
  providerGives,
  providerName,
  providerReach,
} from './connection-labels';

/** A connection with how many accounts it found (null when that read failed). */
interface ConnectionRow {
  connection: ConnectionView;
  accounts: number | null;
}

/** Where the provider's hosted sign-in sends the browser back. */
export const CALLBACK_PATH = '/connections/callback';

/**
 * The trader's broker connections (sync of positions, cash and activity)
 * and the providers an admin turned on. Connecting takes API keys typed
 * here, or a hosted sign-in on the provider's site. "When Stonks trades"
 * says exactly when a connection that can trade places orders (F53): only
 * for Approve each trade and Automatic follows, manual orders and approved
 * suggestions in the linked portfolio, at the portfolio's stage.
 */
@Component({
  selector: 'app-connections-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    PermissionNote,
    EmptyState,
    ErrorState,
    LoadingState,
    StatusPill,
    RouterLink,
    AgoPipe,
    HelpTip,
  ],
  templateUrl: './connections.page.html',
  styleUrl: './connections.page.scss',
})
export class ConnectionsPage {
  private readonly api = inject(ConnectionsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);
  private readonly redirect = inject(BROWSER_REDIRECT);
  protected readonly session = inject(SessionService);

  private readonly providersHeading = viewChild<ElementRef<HTMLElement>>('providersHeading');

  protected readonly providers = resource({ loader: () => this.api.providers() });
  protected readonly connections = resource({
    loader: async (): Promise<ConnectionRow[]> =>
      (await this.api.list()).map((connection) => ({
        connection,
        accounts: connection.accounts_count ?? null,
      })),
  });
  /** Nothing can be connected until an admin turns a provider on. */
  protected readonly noneEnabled = computed(
    () => this.providers.hasValue() && !this.providers.value().some((p) => p.enabled),
  );

  protected readonly canManage = computed(() => this.session.can('connection.manage'));
  protected readonly statusLabel = CONNECTION_STATUS_LABEL;
  protected readonly gives = providerGives;
  protected readonly flow = providerFlow;
  protected readonly reach = providerReach;
  /** Where an order goes at each portfolio stage, for "When Stonks trades". */
  protected readonly stages = STAGES.map((stage) => ({ stage, ...STAGE_WORDS[stage] }));
  protected readonly fieldLabel = fieldLabel;
  protected readonly offersPaper = offersPaper;

  // ---- connect with keys --------------------------------------------------
  /** The provider whose key form is open. */
  protected readonly keysFor = signal<string | null>(null);
  /** Typed key values; cleared when the form closes. Never logged or echoed. */
  protected readonly keyValues = signal<Record<string, string>>({});
  protected readonly keyLabel = signal('');
  protected readonly paper = signal(true);
  protected readonly keysSubmitted = signal(false);
  protected readonly busy = signal<string | null>(null);

  protected providerName(name: string): string {
    return providerName(this.providers.hasValue() ? this.providers.value() : undefined, name);
  }

  protected showProviders(): void {
    const heading = this.providersHeading()?.nativeElement;
    heading?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    heading?.focus();
  }

  protected toggleKeys(p: ProviderView): void {
    this.resetKeys();
    if (this.keysFor() === p.name) {
      this.keysFor.set(null);
      return;
    }
    this.keysFor.set(p.name);
  }

  protected setKey(field: string, value: string): void {
    this.keyValues.update((v) => ({ ...v, [field]: value }));
  }

  protected missing(p: ProviderView, field: string): boolean {
    return this.keysSubmitted() && !(this.keyValues()[field] ?? '').trim();
  }

  async connectWithKeys(p: ProviderView): Promise<void> {
    this.keysSubmitted.set(true);
    const values = this.keyValues();
    if (p.credential_fields.some((f) => !(values[f] ?? '').trim()) || this.busy()) return;
    const ok = await this.confirm.confirm({
      title: `Connect ${p.display_name}?`,
      message:
        'Stonks checks the keys, then reads your accounts. It keeps the keys encrypted and never shows them again.',
      confirmLabel: 'Connect',
    });
    if (!ok) return;
    const fields: Record<string, string> = {};
    for (const f of p.credential_fields) fields[f] = values[f].trim();
    if (offersPaper(p)) fields['paper'] = this.paper() ? 'true' : 'false';
    this.busy.set(p.name);
    try {
      const connection = await this.api.connectWithKeys({
        provider: p.name,
        fields,
        label: this.keyLabel().trim() || null,
      });
      this.toasts.success(`Connected ${p.display_name}.`);
      this.resetKeys();
      this.keysFor.set(null);
      await this.router.navigate(['/connections', connection.id]);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }

  async startPortal(p: ProviderView): Promise<void> {
    if (this.busy()) return;
    const ok = await this.confirm.confirm({
      title: `Sign in at ${p.display_name}?`,
      message: `You leave Stonks to sign in on the ${p.display_name} site, then come back here to finish.`,
      confirmLabel: `Continue to ${p.display_name}`,
    });
    if (!ok) return;
    this.busy.set(p.name);
    try {
      const link = await this.api.startPortal({
        provider: p.name,
        redirect_uri: `${this.redirect.origin()}${CALLBACK_PATH}`,
      });
      this.redirect.go(link.url);
    } catch {
      // The error interceptor already showed the API's message.
      this.busy.set(null);
    }
  }

  private resetKeys(): void {
    this.keyValues.set({});
    this.keyLabel.set('');
    this.paper.set(true);
    this.keysSubmitted.set(false);
  }
}
