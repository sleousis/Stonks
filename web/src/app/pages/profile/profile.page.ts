import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { AuthService } from '../../api/auth.service';
import type { ApiScope, TokenView } from '../../api/models';
import { PASSWORD_MAX, PASSWORD_MIN, sameAs } from '../../core/auth/passwords';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { OneTimeSecret } from '../../shared/ui/one-time-secret';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { PortfoliosPanel } from './portfolios-panel';

interface ScopeOption {
  value: ApiScope;
  label: string;
  help: string;
  adminOnly?: boolean;
}

const SCOPES: readonly ScopeOption[] = [
  { value: 'read', label: 'Read', help: 'See your portfolio, strategies and data.' },
  { value: 'trade', label: 'Trade', help: 'Change modes and place orders. Asks for a code.' },
  { value: 'lab', label: 'Lab', help: 'Run backtests and lab jobs.' },
  { value: 'admin', label: 'Admin', help: 'Admin actions. Asks for a code.', adminOnly: true },
];

const EXPIRY: readonly { days: number | null; label: string }[] = [
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
  { days: 365, label: '1 year' },
  { days: null, label: 'Never' },
];

const ROLE_LABELS: Record<string, string> = {
  viewer: 'Viewer',
  trader: 'Trader',
  admin: 'Admin',
};

/**
 * Your account: password, recovery codes and API tokens. Sensitive changes
 * ask for a fresh code (the session interceptor opens the prompt when the
 * API asks for it). New secrets are shown once.
 */
@Component({
  selector: 'app-profile-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    ReactiveFormsModule,
    PageHeader,
    OneTimeSecret,
    StatusPill,
    LoadingState,
    EmptyState,
    ErrorState,
    PortfoliosPanel,
  ],
  templateUrl: './profile.page.html',
  styleUrl: './profile.page.scss',
})
export class ProfilePage {
  private readonly authApi = inject(AuthService);
  protected readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly fb = inject(NonNullableFormBuilder);

  protected readonly passwordMin = PASSWORD_MIN;
  protected readonly expiry = EXPIRY;
  protected readonly me = this.session.me;
  protected readonly viaSession = this.session.viaSession;
  protected readonly roleLabel = computed(() => ROLE_LABELS[this.me()?.role ?? ''] ?? 'Unknown');
  protected readonly scopes = computed(() =>
    SCOPES.filter((s) => !s.adminOnly || this.session.isAdmin()),
  );

  // Password ------------------------------------------------------------------
  protected readonly passwordForm = this.fb.group(
    {
      current: ['', [Validators.required]],
      next: [
        '',
        [
          Validators.required,
          Validators.minLength(PASSWORD_MIN),
          Validators.maxLength(PASSWORD_MAX),
        ],
      ],
      repeat: ['', [Validators.required]],
    },
    { validators: sameAs('next', 'repeat') },
  );

  // Which password field shows an error, for aria-invalid and aria-describedby (UX-61).
  protected currentInvalid(): boolean {
    const c = this.passwordForm.controls.current;
    return c.touched && c.invalid;
  }

  protected nextInvalid(): boolean {
    const c = this.passwordForm.controls.next;
    return c.touched && c.invalid;
  }

  protected repeatInvalid(): boolean {
    return this.passwordForm.controls.repeat.touched && this.passwordForm.hasError('mismatch');
  }

  protected readonly savingPassword = signal(false);

  // Recovery codes --------------------------------------------------------------
  protected readonly newCodes = signal<string[] | null>(null);
  protected readonly makingCodes = signal(false);

  // Tokens ------------------------------------------------------------------------
  /** Keyed on the user id, so a step-up (which refreshes `me`) does not reload. */
  private readonly userId = computed(() => this.me()?.user_id);
  protected readonly tokens = resource({
    params: () => this.userId(),
    loader: () => this.authApi.tokens(),
  });
  protected readonly activeTokens = computed(() =>
    this.tokens.hasValue() ? this.tokens.value().filter((t) => !t.revoked_at) : [],
  );
  protected readonly tokenForm = this.fb.group({
    name: ['', [Validators.required, Validators.maxLength(100)]],
    read: [true],
    trade: [false],
    lab: [false],
    admin: [false],
    expires: ['90'],
  });
  protected readonly createdToken = signal<string | null>(null);
  protected readonly creatingToken = signal(false);
  protected readonly revoking = signal<string | null>(null);

  protected async changePassword(): Promise<void> {
    if (this.passwordForm.invalid) {
      this.passwordForm.markAllAsTouched();
      return;
    }
    const { current, next } = this.passwordForm.getRawValue();
    this.savingPassword.set(true);
    try {
      await this.authApi.changePassword({ current_password: current, new_password: next });
      this.passwordForm.reset();
      this.toasts.success('Password changed. Your other devices are signed out.');
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.savingPassword.set(false);
    }
  }

  protected async makeRecoveryCodes(): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Make new recovery codes?',
      message: 'Your old codes stop working. The new ones are shown once.',
      confirmLabel: 'Make new codes',
    });
    if (!ok) return;
    this.makingCodes.set(true);
    try {
      const { recovery_codes } = await this.authApi.regenerateRecoveryCodes();
      this.newCodes.set(recovery_codes);
      this.toasts.success('New recovery codes made. Save them now.');
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.makingCodes.set(false);
    }
  }

  protected async createToken(): Promise<void> {
    const v = this.tokenForm.getRawValue();
    const scopes = this.scopes()
      .map((s) => s.value)
      .filter((s) => v[s]);
    if (this.tokenForm.invalid || scopes.length === 0) {
      this.tokenForm.markAllAsTouched();
      return;
    }
    this.creatingToken.set(true);
    try {
      const created = await this.authApi.createToken({
        name: v.name.trim(),
        scopes,
        expires_in_days: v.expires === 'never' ? null : Number(v.expires),
      });
      this.createdToken.set(created.token);
      this.tokenForm.reset();
      this.tokens.reload();
      this.toasts.success(`Token "${created.info.name}" created. Copy it now.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.creatingToken.set(false);
    }
  }

  protected async revoke(token: TokenView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Revoke "${token.name}"?`,
      message: 'Scripts and tools using this token stop working at once.',
      confirmLabel: 'Revoke token',
      tone: 'danger',
    });
    if (!ok) return;
    this.revoking.set(token.id);
    try {
      await this.authApi.revokeToken(token.id);
      this.tokens.reload();
      this.toasts.success(`Revoked "${token.name}".`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.revoking.set(null);
    }
  }

  /** Signs out and reloads on the sign-in page (UX-07). */
  protected async signOut(): Promise<void> {
    await this.session.logout();
  }

  protected scopeLabel(token: TokenView): string {
    return token.scopes.map((s) => SCOPES.find((o) => o.value === s)?.label ?? s).join(', ');
  }

  protected tokenDates(token: TokenView): string {
    const parts = [`Made ${formatDate(token.created_at)}`];
    parts.push(token.last_used_at ? `used ${formatDate(token.last_used_at)}` : 'never used');
    parts.push(token.expires_at ? `ends ${formatDate(token.expires_at)}` : 'no end date');
    return parts.join(', ');
  }

  protected tokenError(): string | null {
    const f = this.tokenForm;
    if (!f.touched) return null;
    if (f.controls.name.invalid) return 'Give the token a name.';
    if (!this.scopes().some((s) => f.controls[s.value].value)) return 'Pick at least one scope.';
    return null;
  }
}
