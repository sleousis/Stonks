import { TestBed } from '@angular/core/testing';

import { ApiError } from '../http/api-error';
import { NoopStepUpService, StepUpService, isStepUpRequired } from './step-up.service';

describe('StepUpService', () => {
  it('defaults to a no-op that lets the API decide', async () => {
    const stepUp = TestBed.inject(StepUpService);
    expect(stepUp).toBeInstanceOf(NoopStepUpService);
    expect(await stepUp.ensure('Resume trading')).toBe(true);
    // Without a prompt it cannot answer a forced step-up.
    expect(await stepUp.ensure('Resume trading', { force: true })).toBe(false);
  });

  it('spots the API asking for a fresh second factor', () => {
    expect(isStepUpRequired(new ApiError(403, 'Forbidden', 'step_up_required: verify'))).toBe(true);
    expect(isStepUpRequired(new ApiError(403, 'Forbidden', 'forbidden: admins only'))).toBe(false);
    expect(isStepUpRequired(new ApiError(401, 'Unauthorized', 'step_up_required'))).toBe(false);
    expect(isStepUpRequired(null)).toBe(false);
  });
});
