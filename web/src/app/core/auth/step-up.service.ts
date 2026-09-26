import { Injectable } from '@angular/core';

/**
 * Asks for a fresh second factor before a sensitive action (resume trading).
 * `ensure()` resolves true when the session has a recent step-up (or the
 * trader just gave one), false when they cancelled or none can be had.
 *
 * This is a placeholder seam: the default never prompts and lets the API
 * decide. The sign-in work replaces it with a dialog that sends a TOTP or
 * recovery code to `POST /api/auth/mfa/verify`:
 *
 *   { provide: StepUpService, useClass: TotpStepUpService }
 *
 * `force` asks again even when the window looks open (the API answered 403
 * `step_up_required`).
 */
@Injectable({ providedIn: 'root', useFactory: () => new NoopStepUpService() })
export abstract class StepUpService {
  abstract ensure(reason: string, options?: { force?: boolean }): Promise<boolean>;
}

/** Default: never prompts. Without a prompt it cannot satisfy a forced step-up. */
export class NoopStepUpService extends StepUpService {
  ensure(_reason: string, options?: { force?: boolean }): Promise<boolean> {
    return Promise.resolve(!options?.force);
  }
}

/** True when the API refused because the second factor is not fresh. */
export function isStepUpRequired(error: unknown): boolean {
  const e = error as { status?: unknown; message?: unknown } | null;
  return e?.status === 403 && typeof e.message === 'string' && e.message.includes('step_up');
}
