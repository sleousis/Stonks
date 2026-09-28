import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { map } from 'rxjs';

import { IngestService } from '../../api/ingest.service';
import { AuthService } from '../../api/auth.service';
import { LabService } from '../../api/lab.service';
import type { AssetClassCosts, CostModelPreset, RiskPolicy } from '../../api/models';
import { SystemService } from '../../api/system.service';
import { AuthTokenService } from '../../core/auth/auth-token.service';
import { SessionService } from '../../core/auth/session.service';
import { FeatureFlagsService } from '../../core/features/feature-flags.service';
import { formatDateTime, formatMoney, formatNumber, formatPercent } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { type ThemeMode, ThemeService } from '../../core/theme/theme.service';
import { DisplayPrefs } from '../../shared/ui/display-prefs';
import { RiskLimitsPanel } from '../../shared/ui/risk-limits-panel';
import { PageHeader } from '../../shared/ui/page-header';
import { type PageTab, PageTabs } from '../../shared/ui/page-tabs';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { ModeStamp } from '../../shared/ui/mode-stamp';
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

/** The sections of Settings, in order. System is for admins. */
export type SettingsSection = 'account' | 'alerts' | 'display' | 'risk' | 'system';

const SECTION_WORDS: Record<SettingsSection, string> = {
  account: 'Sign-in, security and API tokens.',
  alerts: 'Where alerts reach you and which ones you get.',
  display: 'Theme, numbers and dates, and keyboard shortcuts.',
  risk: 'The limits every order in your portfolios must pass.',
  system: 'How the server is set up. Only admins see this.',
};

/**
 * Settings, one section at a time (M14): Account, Alerts, Display, Risk
 * limits, and System for admins. The section is in the address
 * (`/settings?tab=alerts`), so other pages can link straight to it.
 */
@Component({
  selector: 'app-settings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DisplayPrefs,
    RiskLimitsPanel,
    PageHeader,
    PageTabs,
    ReactiveFormsModule,
    RouterLink,
    ModeStamp,
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
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);
  private readonly authApi = inject(AuthService);
  private readonly system = inject(SystemService);
  private readonly ingestApi = inject(IngestService);
  private readonly labApi = inject(LabService);
  protected readonly theme = inject(ThemeService);
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  // Sections ------------------------------------------------------------------
  protected readonly sections = computed<PageTab[]>(() => [
    { id: 'account', label: 'Account' },
    { id: 'alerts', label: 'Alerts' },
    { id: 'display', label: 'Display' },
    { id: 'risk', label: 'Risk limits' },
    ...(this.showSystem() ? [{ id: 'system', label: 'System' }] : []),
  ]);
  private readonly tabParam = toSignal(
    this.route.queryParamMap.pipe(map((params) => params.get('tab'))),
    { initialValue: null },
  );
  /** The section on screen: the address's `tab`, when it names one this person may see. */
  protected readonly section = linkedSignal<SettingsSection>(() => {
    const tab = this.tabParam();
    return this.sections().some((s) => s.id === tab) ? (tab as SettingsSection) : 'account';
  });
  protected readonly description = computed(() => SECTION_WORDS[this.section()]);

  protected pick(id: string | null): void {
    const section = (id ?? 'account') as SettingsSection;
    this.section.set(section);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { tab: section === 'account' ? null : section },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  /** The assistant has no model server: System says how to turn it on (F42). */
  protected readonly assistantOff = inject(FeatureFlagsService).assistantOff;

  // Token -------------------------------------------------------------------
  protected readonly hasToken = this.auth.hasToken;
  protected readonly showToken = signal(false);
  protected readonly testing = signal(false);
  protected readonly tokenCheck = signal<TokenCheck | null>(null);

  protected readonly tokenForm = this.fb.group({
    token: ['', [Validators.required, Validators.maxLength(512)]],
  });

  protected readonly themes: readonly { value: ThemeMode; label: string }[] = [
    { value: 'system', label: 'Match the system' },
    { value: 'light', label: 'Light' },
    { value: 'dark', label: 'Dark' },
  ];

  // System (admins only) ---------------------------------------------------
  /**
   * The server's setup: broker, risk policy, data sources and cost models.
   * Only admins see it, and only their browser asks for it.
   */
  protected readonly showSystem = computed(() => this.session.can('operations.run'));
  private readonly systemParams = () => (this.showSystem() ? {} : undefined);

  protected readonly broker = resource({
    params: this.systemParams,
    loader: () => this.system.broker(),
  });
  /** Only asked for when the broker is Alpaca (the route is 409 otherwise). */
  protected readonly alpaca = resource({
    params: () =>
      this.showSystem() && this.broker.hasValue() && this.broker.value().kind === 'alpaca'
        ? { kind: 'alpaca' }
        : undefined,
    loader: () => this.system.alpacaStatus(),
  });
  protected readonly risk = resource({
    params: this.systemParams,
    loader: () => this.system.riskPolicy(),
  });
  protected readonly sources = resource({
    params: this.systemParams,
    loader: () => this.ingestApi.sources(),
  });
  protected readonly costModels = resource({
    params: this.systemParams,
    loader: () => this.labApi.costModels(),
  });

  protected sourceName(id: string): string {
    return SOURCE_NAMES[id] ?? capitalize(id);
  }

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
  /** Save the token, then ask who it signs in as, and say so (UX-37). */
  protected async saveToken(): Promise<void> {
    if (this.tokenForm.invalid) {
      this.tokenForm.markAllAsTouched();
      return;
    }
    this.auth.setToken(this.tokenForm.controls.token.value);
    this.tokenForm.reset();
    this.showToken.set(false);
    this.tokenCheck.set(null);
    const status = await this.session.load(true);
    const me = this.session.me();
    if (status === 'signed-in' && me) {
      this.toasts.success(
        `Token saved for this tab. You are ${me.display_name}, ${me.role}.`,
        'Token saved',
      );
      return;
    }
    // A token the server refuses would sign this tab out: drop it again.
    this.auth.clear();
    await this.session.load(true);
    this.toasts.error('That token did not work, so it was not kept. Check it and try again.');
  }

  /** Forget the token, then ask who this tab is without it (UX-37). */
  protected async clearToken(): Promise<void> {
    this.auth.clear();
    this.tokenCheck.set(null);
    const status = await this.session.load(true);
    const me = this.session.me();
    this.toasts.info(
      status === 'signed-in' && me
        ? `Token removed. You are ${me.display_name}, ${me.role}.`
        : 'Token removed. Sign in to keep using the console.',
    );
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

/** Display names for the data sources the server knows. */
const SOURCE_NAMES: Record<string, string> = {
  eodhd: 'EODHD',
  yahoo: 'Yahoo Finance',
  defillama: 'DefiLlama',
};

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
