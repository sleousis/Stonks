import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';

import type { MfaCodeRequest } from '../../api/models';
import { ToastService } from '../notify/toast.service';
import { SessionService } from './session.service';

export interface StepUpRequest {
  /** What the code unlocks, in a few words ("Turn on auto for momentum"). */
  reason: string;
}

const DEFAULT_REASON = 'This action needs a fresh code from your authenticator app.';

/**
 * The step-up prompt: a fresh second factor before a sensitive action
 * (auto mode, password, recovery codes, user changes, tokens with trade).
 *
 * Two ways in, both rendered by `<app-step-up-dialog>` in the shell:
 * - `ensure()` before the action, when the page knows it needs one;
 * - the session interceptor, when the API answers 403 `step_up_required`;
 *   it prompts, then retries the request once.
 */
@Injectable({ providedIn: 'root' })
export class StepUpService {
  private readonly session = inject(SessionService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);

  private readonly current = signal<StepUpRequest | null>(null);
  readonly request = this.current.asReadonly();
  private pending: { promise: Promise<boolean>; resolve: (ok: boolean) => void } | null = null;

  /** Ask for a code. True once one is accepted, false when cancelled. */
  prompt(reason = DEFAULT_REASON): Promise<boolean> {
    if (!this.session.viaSession()) {
      this.toasts.error(
        this.session.me()
          ? 'Sign in to the console with your password to do this. A token cannot confirm a code.'
          : 'Sign in to do this.',
        'Sign-in needed',
      );
      return Promise.resolve(false);
    }
    if (this.pending) return this.pending.promise;
    let resolve!: (ok: boolean) => void;
    const promise = new Promise<boolean>((r) => (resolve = r));
    this.pending = { promise, resolve };
    this.current.set({ reason });
    return promise;
  }

  /**
   * Before a sensitive action: prompt only when the last check is too old.
   * A signed-out answer goes to sign-in; a network blip keeps the user and
   * still prompts (the code check then says what went wrong).
   */
  async ensure(reason = DEFAULT_REASON): Promise<boolean> {
    const status = await this.session.load(true);
    if (status === 'signed-out' || status === 'mfa-pending') {
      const here = this.router.url;
      void this.router.navigate(['/login'], {
        queryParams: status === 'mfa-pending' ? { step: 'code', next: here } : { next: here },
      });
      return false;
    }
    if (this.session.me()?.mfa_fresh) return true;
    return this.prompt(reason);
  }

  /** From the dialog. Throws the ApiError of a wrong code and stays open. */
  async submit(body: MfaCodeRequest): Promise<void> {
    await this.session.verify(body);
    this.finish(true);
  }

  cancel(): void {
    this.finish(false);
  }

  private finish(ok: boolean): void {
    const pending = this.pending;
    this.pending = null;
    this.current.set(null);
    pending?.resolve(ok);
  }
}
