import { Injectable, inject } from '@angular/core';

import { HaltsService, RESUME_CONFIRMATION } from '../../api/halts.service';
import type { HaltView } from '../../api/models';
import { StepUpService } from '../auth/step-up.service';

/** What the person confirmed on the resume sheet. */
export interface KillResumeAnswer {
  reason: string;
  overrideChecks: boolean;
}

/**
 * The one way to lift kill switches, for the Halts page and the session
 * strip alike. The server wants a fresh code for every resume, paper or
 * real money, so the code comes first (only when the last one is too old)
 * and each switch then takes a single request.
 */
@Injectable({ providedIn: 'root' })
export class KillResumeService {
  private readonly api = inject(HaltsService);
  private readonly stepUp = inject(StepUpService);

  /** True once every switch is resumed, false when the code is cancelled. API errors throw. */
  async resume(halts: readonly HaltView[], answer: KillResumeAnswer): Promise<boolean> {
    if (!halts.length) return false;
    if (!(await this.stepUp.ensure('Resume trading'))) return false;
    for (const h of halts) {
      await this.api.resume(h.id, {
        confirmation: RESUME_CONFIRMATION,
        reason: answer.reason,
        ...(answer.overrideChecks ? { override_checks: true } : {}),
      });
    }
    return true;
  }
}
