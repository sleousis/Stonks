import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';

import type { Role, UserView } from '../../api/models';
import { UsersService } from '../../api/users.service';
import { PASSWORD_MAX, PASSWORD_MIN } from '../../core/auth/passwords';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatAgo } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PageHeader } from '../../shared/ui/page-header';
import { Sheet, TypedConfirm, typedMatches } from '../../shared/ui/sheet';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

export const ROLES: readonly { value: Role; label: string; help: string }[] = [
  { value: 'viewer', label: 'Viewer', help: 'Sees data and their own portfolio.' },
  { value: 'trader', label: 'Trader', help: 'Trades their own portfolios.' },
  { value: 'admin', label: 'Admin', help: 'Runs the system and manages people.' },
];

/**
 * Admins: the people with an account. Identity, role and status only, never
 * holdings. Every change asks for a fresh code (the API says when).
 */
@Component({
  selector: 'app-admin-users-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReactiveFormsModule,
    PageHeader,
    StatusPill,
    LoadingState,
    EmptyState,
    ErrorState,
    Sheet,
    TypedConfirm,
  ],
  templateUrl: './admin-users.page.html',
  styleUrl: './admin-users.page.scss',
})
export class AdminUsersPage {
  private readonly api = inject(UsersService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly fb = inject(NonNullableFormBuilder);

  protected readonly roles = ROLES;
  protected readonly passwordMin = PASSWORD_MIN;
  protected readonly users = resource({ loader: () => this.api.list() });
  protected readonly sorted = computed(() =>
    this.users.hasValue()
      ? [...this.users.value()].sort((a, b) => a.display_name.localeCompare(b.display_name))
      : [],
  );
  protected readonly myId = computed(() => this.session.me()?.user_id ?? null);

  protected readonly showForm = signal(false);
  protected readonly creating = signal(false);
  protected readonly busy = signal<string | null>(null);
  protected readonly form = this.fb.group({
    display_name: ['', [Validators.required, Validators.maxLength(100)]],
    email: ['', [Validators.required, Validators.email, Validators.maxLength(320)]],
    role: ['trader' as Role, [Validators.required]],
    password: [
      '',
      [Validators.required, Validators.minLength(PASSWORD_MIN), Validators.maxLength(PASSWORD_MAX)],
    ],
  });

  protected async create(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    const v = this.form.getRawValue();
    this.creating.set(true);
    try {
      const user = await this.api.create({ ...v, email: v.email.trim() });
      this.form.reset();
      this.showForm.set(false);
      this.users.reload();
      this.toasts.success(
        `Added ${user.display_name}. Share the password privately. They set up their app at first sign-in.`,
      );
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.creating.set(false);
    }
  }

  protected async changeRole(user: UserView, event: Event): Promise<void> {
    const select = event.target as HTMLSelectElement;
    const role = select.value as Role;
    if (role === user.role) return;
    const label = ROLES.find((r) => r.value === role)?.label ?? role;
    const ok = await this.confirm.confirm({
      title: `Make ${user.display_name} ${article(label)} ${label.toLowerCase()}?`,
      message: ROLES.find((r) => r.value === role)?.help ?? '',
      confirmLabel: 'Change role',
    });
    if (!ok || !(await this.apply(user, { role }, `${user.display_name} is now ${label}.`))) {
      select.value = user.role;
    }
  }

  protected async toggleStatus(user: UserView): Promise<void> {
    const disable = user.status === 'active';
    const ok = await this.confirm.confirm(
      disable
        ? {
            title: `Disable ${user.display_name}?`,
            message:
              'They are signed out everywhere, their tokens stop working and auto trading pauses.',
            confirmLabel: 'Disable',
            tone: 'danger',
          }
        : {
            title: `Enable ${user.display_name}?`,
            message: 'They can sign in again.',
            confirmLabel: 'Enable',
          },
    );
    if (!ok) return;
    await this.apply(
      user,
      { status: disable ? 'disabled' : 'active' },
      `${user.display_name} is ${disable ? 'disabled' : 'enabled'}.`,
    );
  }

  protected async resetMfa(user: UserView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Reset the authenticator for ${user.display_name}?`,
      message: 'Use this when they lost their phone. They set up a new app at their next sign-in.',
      confirmLabel: 'Reset authenticator',
      tone: 'danger',
    });
    if (!ok) return;
    this.busy.set(user.id);
    try {
      await this.api.resetMfa(user.id);
      this.users.reload();
      this.toasts.success(`${user.display_name} sets up a new app at next sign-in.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }

  /** The person whose password is being reset (the sheet is open). */
  protected readonly resetting = signal<UserView | null>(null);
  protected readonly newPassword = signal('');
  protected readonly resetTyped = signal('');
  protected readonly resetBusy = signal(false);
  protected readonly resetPhrase = computed(() => {
    const u = this.resetting();
    return u ? (u.email ?? u.display_name) : '';
  });
  protected readonly canReset = computed(
    () =>
      !this.resetBusy() &&
      this.newPassword().length >= PASSWORD_MIN &&
      this.newPassword().length <= PASSWORD_MAX &&
      typedMatches(this.resetPhrase(), this.resetTyped()),
  );

  protected openReset(user: UserView): void {
    this.newPassword.set('');
    this.resetTyped.set('');
    this.resetting.set(user);
  }

  protected closeReset(): void {
    this.resetting.set(null);
    this.newPassword.set('');
    this.resetTyped.set('');
  }

  protected async resetPassword(): Promise<void> {
    const user = this.resetting();
    if (!user || !this.canReset()) return;
    this.resetBusy.set(true);
    try {
      await this.api.resetPassword(user.id, this.newPassword());
      this.closeReset();
      this.toasts.success(
        `Reset the password for ${user.display_name}. Share it privately. They were signed out.`,
      );
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.resetBusy.set(false);
    }
  }

  protected roleLabel(role: Role): string {
    return ROLES.find((r) => r.value === role)?.label ?? role;
  }

  protected lastSeen(user: UserView): string {
    return user.last_login_at ? `Last sign-in ${formatAgo(user.last_login_at)}` : 'Never signed in';
  }

  private async apply(
    user: UserView,
    body: { role?: Role; status?: 'active' | 'disabled' },
    done: string,
  ): Promise<boolean> {
    this.busy.set(user.id);
    try {
      await this.api.update(user.id, body);
      this.users.reload();
      this.toasts.success(done);
      return true;
    } catch {
      // The error interceptor already showed the API's message.
      return false;
    } finally {
      this.busy.set(null);
    }
  }
}

function article(word: string): string {
  return /^[aeiou]/i.test(word) ? 'an' : 'a';
}
