import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  Injector,
  type OnInit,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';

import type { EnrolStartView } from '../../api/models';
import { safeNext } from '../../core/auth/auth.guards';
import { AuthTokenService } from '../../core/auth/auth-token.service';
import { SessionService } from '../../core/auth/session.service';
import { errorMessage } from '../../core/http/api-error';
import {
  NotificationPermissionService,
  type NotificationState,
  type PushStatus,
} from '../../core/pwa/notification-permission.service';
import { CopyButton } from '../../shared/ui/copy-button';
import { OneTimeSecret } from '../../shared/ui/one-time-secret';
import { QrCode } from './qr-code';

/**
 * - `loading`: a reload mid sign-in, until the next step is known (UX-70).
 * - `password`: email and password (an API token sits under "For scripts").
 * - `enrol`: first login, set up the authenticator app from a QR code.
 * - `verify`: a code from the app, or a recovery code.
 * - `codes`: the ten recovery codes, shown once.
 * - `push`: offer alerts on this device, once, after first login.
 */
export type LoginStep = 'loading' | 'password' | 'enrol' | 'verify' | 'codes' | 'push';

const HEADINGS: Record<LoginStep, string> = {
  loading: 'Signing in',
  password: 'Sign in',
  enrol: 'Set up your authenticator',
  verify: 'Enter your code',
  codes: 'Save your recovery codes',
  push: 'Get alerts on this device?',
};

/**
 * Sign-in: password, then the second factor (set up at first login), then
 * recovery codes and the push offer after a first login. Shown without the
 * app frame (`data.bare`).
 */
@Component({
  selector: 'app-login-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, QrCode, OneTimeSecret, CopyButton],
  templateUrl: './login.page.html',
  styleUrl: './login.page.scss',
})
export class LoginPage implements OnInit {
  private readonly session = inject(SessionService);
  private readonly tokens = inject(AuthTokenService);
  private readonly router = inject(Router);
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly injector = inject(Injector);
  private readonly headingEl = viewChild.required<ElementRef<HTMLElement>>('headingRef');
  protected readonly push = inject(NotificationPermissionService);

  /** `?step=code`: the password is done, ask for the second factor. */
  readonly step = input<string>();
  /** `?next=/path`: where to go afterwards. */
  readonly next = input<string>();

  protected readonly current = signal<LoginStep>('password');
  protected readonly heading = computed(() => HEADINGS[this.current()]);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly enrolment = signal<EnrolStartView | null>(null);
  protected readonly recoveryCodes = signal<string[]>([]);
  protected readonly savedCodes = signal(false);
  protected readonly useRecovery = signal(false);
  /**
   * The API-token form is for scripts and local work: folded away under
   * "For scripts", open only when this server runs with open reads (dev).
   */
  protected readonly tokenPathOpen = computed(() => this.session.status() === 'open');
  /** Why alerts did not turn on, after the trader asked (UX-46). */
  protected readonly pushProblem = signal<string | null>(null);

  protected readonly passwordForm = this.fb.group({
    email: ['', [Validators.required, Validators.maxLength(320)]],
    password: ['', [Validators.required, Validators.maxLength(1024)]],
  });
  protected readonly codeForm = this.fb.group({
    code: ['', [Validators.required, Validators.maxLength(64)]],
  });
  protected readonly tokenForm = this.fb.group({
    token: ['', [Validators.required, Validators.maxLength(512)]],
  });

  /** The secret in groups of four, for typing into the app by hand. */
  protected readonly groupedSecret = computed(
    () =>
      this.enrolment()
        ?.secret.match(/.{1,4}/g)
        ?.join(' ') ?? '',
  );

  ngOnInit(): void {
    const known = this.session.step();
    if (this.step() === 'code' || known) {
      // Mid sign-in: show a short wait, not the password form, until the step is known.
      this.current.set('loading');
      void this.startSecondFactor(known);
    }
  }

  protected async signIn(): Promise<void> {
    if (this.passwordForm.invalid) {
      this.passwordForm.markAllAsTouched();
      return;
    }
    const { email, password } = this.passwordForm.getRawValue();
    await this.run(async () => {
      const next = await this.session.login(email.trim(), password);
      this.passwordForm.controls.password.reset();
      await this.startSecondFactor(next);
    });
  }

  protected async confirmEnrolment(): Promise<void> {
    if (this.codeForm.invalid) {
      this.codeForm.markAllAsTouched();
      return;
    }
    await this.run(async () => {
      const codes = await this.session.confirmEnrolment(this.code());
      this.codeForm.reset();
      this.recoveryCodes.set(codes);
      this.go(codes.length ? 'codes' : 'push');
    });
  }

  protected async verify(): Promise<void> {
    if (this.codeForm.invalid) {
      this.codeForm.markAllAsTouched();
      return;
    }
    const value = this.code();
    await this.run(async () => {
      await this.session.verify(this.useRecovery() ? { recovery_code: value } : { code: value });
      this.codeForm.reset();
      await this.finish();
    });
  }

  protected toggleRecovery(): void {
    this.useRecovery.update((v) => !v);
    this.codeForm.reset();
    this.error.set(null);
  }

  protected afterCodes(): void {
    const state = this.push.state();
    if (state === 'unsupported' || state === 'denied' || state === 'granted') {
      void this.finish();
    } else {
      this.go('push');
    }
  }

  /** Continue only once alerts are really on; otherwise say why (UX-46). */
  protected async enablePush(): Promise<void> {
    this.pushProblem.set(null);
    await this.push.enable();
    if (this.push.push() === 'on') {
      await this.finish();
      return;
    }
    this.pushProblem.set(
      this.push.error()
        ? `Alerts did not turn on. ${LATER}`
        : pushProblemText(this.push.state(), this.push.push()),
    );
  }

  protected skipPush(): void {
    void this.finish();
  }

  /** Token mode (local dev, scripts): save the token for this tab and check it. */
  protected async useToken(): Promise<void> {
    if (this.tokenForm.invalid) {
      this.tokenForm.markAllAsTouched();
      return;
    }
    await this.run(async () => {
      this.tokens.setToken(this.tokenForm.controls.token.value);
      const status = await this.session.load(true);
      if (status !== 'signed-in') {
        this.tokens.clear();
        throw new Error('That token did not work. Check it and try again.');
      }
      this.tokenForm.reset();
      await this.finish();
    });
  }

  protected startOver(): void {
    this.codeForm.reset();
    this.enrolment.set(null);
    this.go('password');
  }

  /**
   * Enrol or verify. When we don't know which (a reload mid sign-in), asking
   * for a new secret answers it: that only works while no app is set up.
   */
  private async startSecondFactor(known: 'enrol' | 'verify' | null): Promise<void> {
    if (known === 'verify') {
      this.go('verify');
      return;
    }
    try {
      this.enrolment.set(await this.session.startEnrolment());
      this.go('enrol');
    } catch (err) {
      if (known === 'enrol') {
        this.go('password');
        this.error.set(errorMessage(err));
      } else {
        this.go('verify');
      }
    }
  }

  private code(): string {
    return this.codeForm.controls.code.value.replace(/\s+/g, '');
  }

  private go(step: LoginStep): void {
    this.error.set(null);
    if (this.current() === step) return;
    this.current.set(step);
    // Screen readers and keyboards land on the new step's heading.
    afterNextRender(() => this.headingEl().nativeElement.focus(), { injector: this.injector });
  }

  private async finish(): Promise<void> {
    await this.router.navigateByUrl(safeNext(this.next()));
  }

  private async run(action: () => Promise<void>): Promise<void> {
    if (this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      await action();
    } catch (err) {
      this.error.set(errorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }
}

const LATER = 'You can turn them on later in Settings.';

/** Why alerts are still off after the trader asked for them, in one line. */
function pushProblemText(state: NotificationState, push: PushStatus): string {
  if (state === 'denied') return `Alerts are blocked for this site in your browser. ${LATER}`;
  if (push === 'waiting-for-server') {
    return `This server cannot send alerts yet. Ask your admin. ${LATER}`;
  }
  if (push === 'no-worker') {
    return `Alerts need the installed app or a secure connection. ${LATER}`;
  }
  if (state === 'unsupported') return `This browser cannot show alerts. ${LATER}`;
  return `Your browser did not allow alerts. ${LATER}`;
}
