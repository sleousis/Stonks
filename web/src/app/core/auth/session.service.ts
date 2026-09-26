import { Injectable, computed, inject, signal } from '@angular/core';

import { AuthService } from '../../api/auth.service';
import type { MeView, MfaCodeRequest } from '../../api/models';
import { ApiError } from '../http/api-error';
import { AuthTokenService } from './auth-token.service';

/**
 * - `unknown`: not asked yet.
 * - `signed-in`: a session, an API token or the legacy token (`me` is set).
 * - `mfa-pending`: password accepted, the second factor is still missing.
 * - `open`: nobody signed in, but this API answers reads (dev profile).
 * - `signed-out`: sign in to continue.
 * - `unreachable`: the API did not answer (pages show their own errors).
 */
export type SessionStatus =
  | 'unknown'
  | 'signed-in'
  | 'mfa-pending'
  | 'open'
  | 'signed-out'
  | 'unreachable';

/** What the second-factor screen asks for: set up the app, or send a code. */
export type SecondFactorStep = 'enrol' | 'verify';

const CSRF_COOKIE = 'stonks_csrf';

/**
 * Who is using the console, and the browser session's sign-in steps.
 *
 * The session itself is an HttpOnly cookie the console never sees. This
 * service keeps the CSRF token (from the sign-in responses, else the
 * readable `stonks_csrf` cookie after a reload) for the session
 * interceptor, and the caller's identity from `GET /api/auth/me`.
 * An API token saved in Settings (token mode) wins over the session, as it
 * does on the server.
 */
@Injectable({ providedIn: 'root' })
export class SessionService {
  private readonly api = inject(AuthService);
  private readonly tokens = inject(AuthTokenService);

  private readonly meSignal = signal<MeView | null>(null);
  private readonly statusSignal = signal<SessionStatus>('unknown');
  private readonly stepSignal = signal<SecondFactorStep | null>(null);
  private readonly csrf = signal<string | null>(null);
  private readonly enrolled = signal(false);
  private loading: Promise<SessionStatus> | null = null;

  readonly me = this.meSignal.asReadonly();
  readonly status = this.statusSignal.asReadonly();
  /** The second-factor step the last sign-in asked for, when known. */
  readonly step = this.stepSignal.asReadonly();
  /** True right after first-login enrolment (the push prompt follows). */
  readonly justEnrolled = this.enrolled.asReadonly();

  readonly role = computed(() => this.meSignal()?.role ?? null);
  readonly isAdmin = computed(() => this.role() === 'admin');
  readonly canTrade = computed(() => this.role() === 'trader' || this.role() === 'admin');
  /** Signed in through the browser session (step-up works only here). */
  readonly viaSession = computed(() => this.meSignal()?.via === 'session');
  readonly signedIn = computed(() => this.statusSignal() === 'signed-in');

  /** The token for `X-CSRF-Token` on unsafe requests made with the session cookie. */
  csrfToken(): string | null {
    return this.csrf() ?? readCookie(CSRF_COOKIE);
  }

  /** Ask the API who we are, once; `force` asks again. */
  load(force = false): Promise<SessionStatus> {
    if (!force && this.statusSignal() !== 'unknown') return Promise.resolve(this.statusSignal());
    this.loading ??= this.fetch().finally(() => (this.loading = null));
    return this.loading;
  }

  /** Password step. Resolves to the second-factor step that comes next. */
  async login(email: string, password: string): Promise<SecondFactorStep> {
    const view = await this.api.login(email, password);
    this.csrf.set(view.csrf_token);
    this.meSignal.set(null);
    this.statusSignal.set('mfa-pending');
    this.stepSignal.set(view.next_step);
    return view.next_step;
  }

  /** A new authenticator secret, while no second factor is set up. */
  startEnrolment() {
    return this.api.startEnrolment();
  }

  /** Confirm the authenticator. Finishes sign-in; returns the recovery codes (shown once). */
  async confirmEnrolment(code: string): Promise<string[]> {
    const res = await this.api.confirmEnrolment(code);
    if (res.csrf_token) this.csrf.set(res.csrf_token);
    this.enrolled.set(true);
    await this.load(true);
    return res.recovery_codes ?? [];
  }

  /** Finish sign-in with a code, or refresh the step-up window when signed in. */
  async verify(body: MfaCodeRequest): Promise<void> {
    const res = await this.api.verify(body);
    if (res.csrf_token) this.csrf.set(res.csrf_token);
    if (this.statusSignal() === 'signed-in' && this.meSignal()) {
      this.meSignal.update((me) => (me ? { ...me, mfa_fresh: true } : me));
    } else {
      await this.load(true);
    }
  }

  /** End the session (and forget this tab's API token). Never throws. */
  async logout(): Promise<void> {
    try {
      await this.api.logout();
    } catch {
      // Already signed out on the server: nothing to undo.
    }
    this.tokens.clear();
    this.csrf.set(null);
    this.enrolled.set(false);
    this.markSignedOut();
  }

  /** The API said the session is gone (expired, revoked). */
  markSignedOut(): void {
    this.meSignal.set(null);
    this.stepSignal.set(null);
    this.statusSignal.set('signed-out');
  }

  /** The API said the second factor is still missing. */
  markMfaPending(): void {
    this.meSignal.set(null);
    this.statusSignal.set('mfa-pending');
  }

  private async fetch(): Promise<SessionStatus> {
    let status: SessionStatus;
    try {
      this.meSignal.set(await this.api.me());
      status = 'signed-in';
    } catch (err) {
      this.meSignal.set(null);
      if (err instanceof ApiError && err.code === 'mfa_required') status = 'mfa-pending';
      else if (err instanceof ApiError && err.isNetwork) status = 'unreachable';
      else status = (await this.api.readsAreOpen()) ? 'open' : 'signed-out';
    }
    this.statusSignal.set(status);
    return status;
  }
}

function readCookie(name: string): string | null {
  try {
    for (const part of document.cookie.split(';')) {
      const [key, ...rest] = part.trim().split('=');
      if (key === name) return decodeURIComponent(rest.join('=')) || null;
    }
  } catch {
    // No document (tests, workers): no cookie.
  }
  return null;
}
