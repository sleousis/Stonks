import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';

import { IngestService } from '../../api/ingest.service';
import { AuthService } from '../../api/auth.service';
import { LabService } from '../../api/lab.service';
import type { AssetClassCosts, CostModelPreset, RiskPolicy } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { AuthTokenService } from '../../core/auth/auth-token.service';
import { formatDateTime, formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { type ThemeMode, ThemeService } from '../../core/theme/theme.service';
import { DisplayPrefs } from '../../shared/ui/display-prefs';
import { NotificationPrefs } from '../../shared/ui/notification-prefs';
import { NotificationSettings } from '../../shared/ui/notification-settings';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { NO_TOKEN, TOKEN_ACCEPTED, type TokenCheck, tokenCheckFromError } from './token-check';

interface Fact {
  label: string;
  value: string;
}

interface CostRow {
  scope: string;
  fee: string;
  spread: string;
  flat: string;
}

@Component({
  selector: 'app-settings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DisplayPrefs,
    NotificationPrefs,
    NotificationSettings,
    PageHeader,
    ReactiveFormsModule,
    StatusPill,
    LoadingState,
    ErrorState,
    EmptyState,
  ],
  templateUrl: './settings.page.html',
  styleUrl: './settings.page.scss',
})
export class SettingsPage {
  private readonly auth = inject(AuthTokenService);
  private readonly toasts = inject(ToastService);
  private readonly authApi = inject(AuthService);
  private readonly system = inject(SystemService);
  private readonly ingestApi = inject(IngestService);
  private readonly labApi = inject(LabService);
  protected readonly theme = inject(ThemeService);
  private readonly fb = inject(NonNullableFormBuilder);

  // Token -------------------------------------------------------------------
  protected readonly hasToken = this.auth.hasToken;
  protected readonly showToken = signal(false);
  protected readonly testing = signal(false);
  protected readonly tokenCheck = signal<TokenCheck | null>(null);

  protected readonly tokenForm = this.fb.group({
    token: ['', [Validators.required, Validators.maxLength(512)]],
  });

  protected readonly readsForm = this.fb.group({
    sendOnReads: [this.auth.sendOnReads()],
  });

  protected readonly themes: readonly { value: ThemeMode; label: string }[] = [
    { value: 'system', label: 'Match the system' },
    { value: 'light', label: 'Light' },
    { value: 'dark', label: 'Dark' },
  ];

  // Read-only configuration -------------------------------------------------
  protected readonly broker = resource({ loader: () => this.system.broker() });
  /** Only asked for when the broker is Alpaca (the route is 409 otherwise). */
  protected readonly alpaca = resource({
    params: () =>
      this.broker.hasValue() && this.broker.value().kind === 'alpaca'
        ? { kind: 'alpaca' }
        : undefined,
    loader: () => this.system.alpacaStatus(),
  });
  protected readonly risk = resource({ loader: () => this.system.riskPolicy() });
  protected readonly sources = resource({ loader: () => this.ingestApi.sources() });
  protected readonly costModels = resource({ loader: () => this.labApi.costModels() });

  protected readonly brokerFacts = computed<Fact[]>(() => {
    if (!this.broker.hasValue()) return [];
    const b = this.broker.value();
    return [
      { label: 'Kind', value: b.kind === 'alpaca' ? 'Alpaca' : 'Simulated' },
      { label: 'Account', value: b.paper ? 'Paper' : 'Live' },
      { label: 'Credentials', value: b.credentials_configured ? 'Configured' : 'Not configured' },
      { label: 'Live trading', value: b.allow_live ? 'Allowed' : 'Not allowed' },
    ];
  });

  protected readonly alpacaFacts = computed<Fact[]>(() => {
    if (!this.alpaca.hasValue()) return [];
    const { account, clock } = this.alpaca.value();
    const facts: Fact[] = [];
    if (account) {
      facts.push(
        { label: 'Account status', value: account.status },
        { label: 'Equity', value: formatMoney(account.equity) },
        { label: 'Cash', value: formatMoney(account.cash) },
        { label: 'Buying power', value: formatMoney(account.buying_power) },
        {
          label: 'Trading',
          value: account.trading_blocked
            ? 'Blocked'
            : account.can_trade
              ? 'Allowed'
              : 'Not allowed',
        },
      );
    }
    if (clock) {
      facts.push({
        label: 'Market',
        value: clock.is_open
          ? `Open, closes ${formatDateTime(clock.next_close)}`
          : `Closed, opens ${formatDateTime(clock.next_open)}`,
      });
    }
    return facts;
  });

  protected readonly riskFacts = computed<Fact[]>(() =>
    this.risk.hasValue() ? riskFacts(this.risk.value()) : [],
  );

  protected costRows(preset: CostModelPreset): CostRow[] {
    const s = preset.settings;
    const rows: CostRow[] = [costRow('Default', s.default)];
    for (const [cls, costs] of Object.entries(s.asset_classes ?? {})) {
      rows.push(costRow(capitalize(cls), costs));
    }
    return rows;
  }

  protected impact(preset: CostModelPreset): string {
    const s = preset.settings;
    return `${formatNumber(s.impact_bps ?? 0)} bps, capped at ${formatNumber(s.max_impact_bps ?? 500)} bps`;
  }

  // Actions -----------------------------------------------------------------
  protected saveToken(): void {
    if (this.tokenForm.invalid) {
      this.tokenForm.markAllAsTouched();
      return;
    }
    this.auth.setToken(this.tokenForm.controls.token.value);
    this.tokenForm.reset();
    this.showToken.set(false);
    this.tokenCheck.set(null);
    this.toasts.success('API token saved for this browser tab.', 'Token saved');
  }

  protected clearToken(): void {
    this.auth.clear();
    this.tokenCheck.set(null);
    this.toasts.info('API token removed. The console is read-only until you enter it again.');
  }

  /** Confirms the saved token against the API without changing anything. */
  protected async testToken(): Promise<void> {
    if (!this.auth.hasToken()) {
      this.tokenCheck.set(NO_TOKEN);
      return;
    }
    this.testing.set(true);
    try {
      await this.authApi.check();
      this.tokenCheck.set(TOKEN_ACCEPTED);
    } catch (err) {
      this.tokenCheck.set(tokenCheckFromError(err));
    } finally {
      this.testing.set(false);
    }
  }

  protected toggleReads(): void {
    this.auth.setSendOnReads(this.readsForm.controls.sendOnReads.value);
  }

  protected setTheme(mode: ThemeMode): void {
    this.theme.setMode(mode);
  }

  protected reloadConfig(): void {
    this.broker.reload();
    this.alpaca.reload();
    this.risk.reload();
    this.sources.reload();
    this.costModels.reload();
  }
}

function riskFacts(r: RiskPolicy): Fact[] {
  const facts: Fact[] = [
    { label: 'Limits', value: r.enabled === false ? 'Off' : 'On' },
    {
      label: 'Max weight per ticker',
      value: formatPercent(r.max_weight_per_ticker ?? 1, { digits: 1 }),
    },
    {
      label: 'Max open positions',
      value: r.max_open_positions == null ? 'No limit' : formatNumber(r.max_open_positions),
    },
    { label: 'Cash buffer', value: formatPercent(r.cash_buffer_fraction ?? 0, { digits: 1 }) },
    {
      label: 'Min order size',
      value: r.min_order_notional ? formatMoney(r.min_order_notional) : 'None',
    },
  ];
  for (const [cls, w] of Object.entries(r.max_weight_per_asset_class ?? {})) {
    facts.push({ label: `Max weight, ${cls}`, value: formatPercent(w, { digits: 1 }) });
  }
  return facts;
}

function costRow(scope: string, c: AssetClassCosts | undefined): CostRow {
  return {
    scope,
    fee: `${formatNumber(c?.fee_bps ?? 0)} bps`,
    spread: `${formatNumber(c?.half_spread_bps ?? 0)} bps`,
    flat: formatMoney(c?.fee_flat ?? 0),
  };
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
