import { Injectable } from '@angular/core';

import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import {
  getOnboarding,
  getSystemChecklist,
  updateOnboarding,
  updateOnboardingStep,
} from './generated/sdk.gen';
import type { OnboardingStepView, StepUpdate } from './models';

export type OnboardingStepId = OnboardingStepView['id'];

/** The first-run guide: your steps (stored per user) and the admin's system checklist. */
@Injectable({ providedIn: 'root' })
export class OnboardingService {
  /** Silent: Today only shows the guide card when this works. */
  get(silent = false) {
    return unwrap(getOnboarding({ headers: silent ? SILENT_HEADERS : undefined }));
  }

  setStep(step: OnboardingStepId, state: StepUpdate['state']) {
    return unwrap(updateOnboardingStep({ path: { step }, body: { state } }));
  }

  /** Close the guide, or bring it back. */
  setDismissed(dismissed: boolean) {
    return unwrap(updateOnboarding({ body: { dismissed } }));
  }

  /** Admins only: data source key, first data load, backup and scheduler. */
  system() {
    return unwrap(getSystemChecklist());
  }
}
