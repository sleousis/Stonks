import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { LiveService } from '../../api/live.service';
import type { AccountProfileBody, AccountProfileView, LiveAllocationView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDateTime, formatMoney } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import {
  ACCOUNT_TYPE_OPTIONS,
  type AccountType,
  CLIENT_CLASS_OPTIONS,
  type ClientClass,
  JURISDICTION_OPTIONS,
  type Jurisdiction,
  accountRuleWords,
  profileNotes,
  safeguardWords,
} from '../../shared/live-rules';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

const REASON_MAX = 500;

/** The allocation form as typed. */
export interface AllocationDraft {
  amount: string;
  currency: string;
  reason: string;
}

export interface AllocationErrors {
  amount?: string;
  currency?: string;
  reason?: string;
}

/** Field errors of the allocation form; empty when it can be sent. */
export function allocationErrors(d: AllocationDraft): AllocationErrors {
  const errors: AllocationErrors = {};
  const amount = Number(d.amount);
  if (d.amount.trim() === '') errors.amount = 'Enter an amount.';
  else if (!Number.isFinite(amount) || amount < 0) errors.amount = 'Enter 0 or more.';
  if (!/^[A-Za-z]{3}$/.test(d.currency.trim())) errors.currency = 'Three letters, like USD.';
  if (!d.reason.trim()) errors.reason = 'Say why. It goes in the audit log.';
  else if (d.reason.length > REASON_MAX) errors.reason = `At most ${REASON_MAX} characters.`;
  return errors;
}

/** The profile a save sends: the three choices, the rest kept as stored. */
export function profileBody(
  current: AccountProfileView | null,
  draft: { jurisdiction: Jurisdiction; account_type: AccountType; client_class: ClientClass },
  baseCurrency: string,
): AccountProfileBody {
  return {
    jurisdiction: draft.jurisdiction,
    account_type: draft.account_type,
    client_class: draft.client_class,
    base_currency: current?.base_currency ?? baseCurrency,
    fx_policy: current?.fx_policy ?? 'refuse',
    wash_sale_mode: current?.wash_sale_mode ?? 'warn',
    // Shorts need a margin account: a switch to cash turns them off.
    allow_short: draft.account_type === 'margin' ? (current?.allow_short ?? false) : false,
  };
}

/** "US, cash account, retail client". */
export function profileText(p: {
  jurisdiction: string;
  account_type?: string;
  client_class?: string;
}): string {
  const type = p.account_type ?? 'cash';
  const kind = p.client_class ?? 'retail';
  const j = JURISDICTION_OPTIONS.find((o) => o.value === p.jurisdiction)?.label ?? p.jurisdiction;
  const t = ACCOUNT_TYPE_OPTIONS.find((o) => o.value === type)?.label ?? type;
  const c = CLIENT_CLASS_OPTIONS.find((o) => o.value === kind)?.label ?? kind;
  return `${j}, ${t.toLowerCase()} account, ${c.toLowerCase()} client`;
}

/**
 * Live settings of one real-money portfolio (`/profile/live/:id`): the
 * allocation the owner lets Stonks trade, set by hand only, the account
 * profile that picks the account rules, and, read only, which live
 * safeguards and account rules act on it. Both changes need a fresh code
 * and are audited.
 */
@Component({
  selector: 'app-live-settings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    ModeStamp,
    PermissionNote,
    Segmented,
    StatusPill,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  templateUrl: './live-settings.page.html',
  styleUrl: './live-settings.page.scss',
})
export class LiveSettingsPage {
  private readonly live = inject(LiveService);
  private readonly confirm = inject(ConfirmService);
  private readonly stepUp = inject(StepUpService);
  private readonly toasts = inject(ToastService);
  protected readonly session = inject(SessionService);
  protected readonly ctx = inject(PortfolioContextService);

  readonly id = input.required<string>();

  protected readonly portfolio = computed(
    () => this.ctx.options().find((p) => p.id === this.id()) ?? null,
  );
  protected readonly isLive = computed(() => this.portfolio()?.trading === 'live');
  protected readonly canManage = computed(() => this.session.can('live.manage'));
  protected readonly reasonMax = REASON_MAX;

  protected readonly allocation = resource({
    params: () => (this.isLive() ? { id: this.id() } : undefined),
    loader: ({ params }) => this.live.allocation(params.id),
  });
  protected readonly profile = resource({
    params: () => (this.isLive() ? { id: this.id() } : undefined),
    loader: ({ params }) => this.live.profile(params.id),
  });
  protected readonly rules = resource({
    params: () => (this.isLive() ? { id: this.id() } : undefined),
    loader: ({ params }) => this.live.rules(params.id),
  });

  // ---- allocation -------------------------------------------------------

  protected readonly amount = linkedSignal(() => {
    const a = this.allocation.hasValue() ? this.allocation.value() : null;
    return a?.amount != null ? String(a.amount) : '';
  });
  protected readonly currency = linkedSignal(() => {
    const a = this.allocation.hasValue() ? this.allocation.value() : null;
    return a?.currency ?? this.portfolio()?.base_currency ?? 'USD';
  });
  protected readonly reason = signal('');
  protected readonly tried = signal(false);
  protected readonly savingAllocation = signal(false);

  protected readonly errors = computed(() =>
    allocationErrors({ amount: this.amount(), currency: this.currency(), reason: this.reason() }),
  );
  protected readonly shownErrors = computed<AllocationErrors>(() =>
    this.tried() ? this.errors() : {},
  );

  protected allocationText(a: LiveAllocationView): string {
    return a.amount == null ? 'Not set' : formatMoney(a.amount, { currency: a.currency ?? 'USD' });
  }

  protected readonly dateTime = formatDateTime;

  async saveAllocation(): Promise<void> {
    this.tried.set(true);
    const p = this.portfolio();
    if (!p || Object.keys(this.errors()).length || this.savingAllocation()) return;
    const amount = Number(this.amount());
    const currency = this.currency().trim().toUpperCase();
    const before = this.allocation.hasValue() ? this.allocation.value() : null;
    const ok = await this.confirm.confirm({
      title: `Set the allocation of ${p.name}?`,
      message:
        'Stonks may hold up to this amount in this real-money account. It stays until you change it: there are no automatic steps.',
      confirmLabel: 'Set allocation',
      tone: 'danger',
      ticket: {
        kind: 'Allocation',
        live: true,
        lines: [
          { label: 'Portfolio', value: p.name },
          { label: 'Now', value: before ? this.allocationText(before) : 'Not set' },
          { label: 'New', value: formatMoney(amount, { currency }) },
          { label: 'Reason', value: this.reason().trim() },
        ],
      },
    });
    if (!ok) return;
    if (!(await this.stepUp.ensure('Set the live allocation'))) return;
    this.savingAllocation.set(true);
    try {
      // A stale second factor comes back as 403 step_up_required: the session
      // interceptor prompts for a code and retries once.
      const saved = await this.live.setAllocation(p.id, {
        amount,
        currency,
        reason: this.reason().trim(),
      });
      this.allocation.set(saved);
      this.reason.set('');
      this.tried.set(false);
      this.rules.reload();
      this.toasts.success(`Set the allocation of ${p.name} to ${this.allocationText(saved)}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.savingAllocation.set(false);
    }
  }

  // ---- account profile --------------------------------------------------

  protected readonly jurisdictions = JURISDICTION_OPTIONS;
  protected readonly accountTypes = ACCOUNT_TYPE_OPTIONS;
  protected readonly clientClasses = CLIENT_CLASS_OPTIONS;

  private readonly stored = computed(() => (this.profile.hasValue() ? this.profile.value() : null));
  protected readonly jurisdiction = linkedSignal<Jurisdiction>(
    () => this.stored()?.jurisdiction ?? 'us',
  );
  protected readonly accountType = linkedSignal<AccountType>(
    () => this.stored()?.account_type ?? 'cash',
  );
  protected readonly clientClass = linkedSignal<ClientClass>(
    () => this.stored()?.client_class ?? 'retail',
  );
  protected readonly savingProfile = signal(false);

  private readonly draft = computed(() => ({
    jurisdiction: this.jurisdiction(),
    account_type: this.accountType(),
    client_class: this.clientClass(),
  }));
  protected readonly notes = computed(() => profileNotes(this.draft()));
  protected readonly profileChanged = computed(() => {
    const s = this.stored();
    const d = this.draft();
    return (
      !s ||
      s.jurisdiction !== d.jurisdiction ||
      s.account_type !== d.account_type ||
      s.client_class !== d.client_class
    );
  });

  protected readonly profileText = profileText;

  async saveProfile(): Promise<void> {
    const p = this.portfolio();
    if (!p || this.savingProfile() || !this.profileChanged()) return;
    const current = this.stored();
    const body = profileBody(current, this.draft(), p.base_currency);
    const ok = await this.confirm.confirm({
      title: `Save the account profile of ${p.name}?`,
      message: 'The account rules of the next trading run follow this profile.',
      confirmLabel: 'Save profile',
      tone: 'danger',
      ticket: {
        kind: 'Account profile',
        live: true,
        lines: [
          { label: 'Portfolio', value: p.name },
          { label: 'Now', value: current ? this.profileText(current) : 'Not set' },
          { label: 'New', value: this.profileText(body) },
        ],
      },
    });
    if (!ok) return;
    if (!(await this.stepUp.ensure('Change the account profile'))) return;
    this.savingProfile.set(true);
    try {
      const saved = await this.live.setProfile(p.id, body);
      this.profile.set(saved);
      this.rules.reload();
      this.toasts.success(`Saved the account profile of ${p.name}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.savingProfile.set(false);
    }
  }

  // ---- rules (read only) -------------------------------------------------

  protected readonly safeguard = safeguardWords;
  protected readonly accountRule = accountRuleWords;

  protected readonly applying = computed(() =>
    this.rules.hasValue() ? this.rules.value().account_rules.filter((r) => r.applies) : [],
  );
  protected readonly notApplying = computed(() =>
    this.rules.hasValue() ? this.rules.value().account_rules.filter((r) => !r.applies) : [],
  );

  constructor() {
    void this.ctx.load();
  }
}
