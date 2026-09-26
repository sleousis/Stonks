import { HttpErrorResponse } from '@angular/common/http';

import { ApiError, errorMessage, toApiError } from './api-error';

describe('toApiError', () => {
  it('uses the problem-details detail as the message', () => {
    const body = { title: 'Conflict', status: 409, detail: 'strategy is already active' };
    const err = toApiError(body, new HttpErrorResponse({ status: 409, error: body }));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.title).toBe('Conflict');
    expect(err.message).toBe('strategy is already active');
  });

  it('falls back to the title when there is no detail', () => {
    const err = toApiError({ title: 'Not Found', status: 404 });
    expect(err.status).toBe(404);
    expect(err.message).toBe('Not Found');
  });

  it('lists field errors from a 422 without the body/query prefix', () => {
    const body = {
      title: 'Invalid request',
      status: 422,
      detail: 'validation failed',
      errors: [
        { loc: ['body', 'tickers', 0], msg: 'String should match pattern', type: 'x' },
        { loc: ['query', 'limit'], msg: 'Input should be greater than 0', type: 'y' },
      ],
    };
    const err = toApiError(body, new HttpErrorResponse({ status: 422, error: body }));
    expect(err.fieldErrors).toEqual([
      { field: 'tickers.0', message: 'String should match pattern' },
      { field: 'limit', message: 'Input should be greater than 0' },
    ]);
    expect(err.message).toBe(
      'validation failed: tickers.0 String should match pattern; limit Input should be greater than 0',
    );
  });

  it('adds a sign-in hint to 401s', () => {
    const body = { title: 'Unauthorized', status: 401, detail: 'missing or invalid bearer token' };
    const err = toApiError(body, new HttpErrorResponse({ status: 401, error: body }));
    expect(err.isAuth).toBe(true);
    expect(err.message).toBe(
      'missing or invalid bearer token. Sign in, or enter an API token in Settings.',
    );
  });

  it('reads the auth code at the start of the detail and says it plainly', () => {
    const cases: [number, string, string, string][] = [
      [401, 'mfa_required: finish signing in', 'mfa_required', 'Finish signing in with your code.'],
      [
        401,
        'not_authenticated: missing or invalid credentials',
        'not_authenticated',
        'You are signed out. Sign in, or enter an API token in Settings.',
      ],
      [401, 'invalid_credentials', 'invalid_credentials', 'That did not match. Try again.'],
      [
        403,
        'step_up_required: users.manage needs a fresh second factor',
        'step_up_required',
        'Confirm it is you with a code from your authenticator app.',
      ],
      [
        429,
        'too_many_attempts: too many failed attempts; try again later',
        'too_many_attempts',
        'Too many tries. Wait a few minutes, then try again.',
      ],
      [
        403,
        'csrf_failed: bad token',
        'csrf_failed',
        'Your session is out of date. Reload the page.',
      ],
      [403, 'forbidden: users.read is not allowed', 'forbidden', 'Your role cannot do this.'],
    ];
    for (const [status, detail, code, message] of cases) {
      const body = { title: 'x', status, detail };
      const err = toApiError(body, new HttpErrorResponse({ status, error: body }));
      expect(err.code).toBe(code);
      expect(err.message).toBe(message);
    }
  });

  it('has no code when the detail does not start with one', () => {
    const body = { title: 'Conflict', status: 409, detail: 'strategy is already active' };
    expect(toApiError(body).code).toBeNull();
  });

  it('explains a network failure (status 0)', () => {
    const err = toApiError(new ProgressEvent('error'), new HttpErrorResponse({ status: 0 }));
    expect(err.isNetwork).toBe(true);
    expect(err.message).toContain('stonks serve');
  });

  it('uses a plain-text error body', () => {
    const err = toApiError('bad gateway', new HttpErrorResponse({ status: 502 }));
    expect(err.message).toBe('bad gateway');
    expect(err.title).toBe('Server error');
  });

  it('accepts an HttpErrorResponse directly', () => {
    const res = new HttpErrorResponse({
      status: 503,
      error: { title: 'Service Unavailable', status: 503, detail: 'API token not configured' },
    });
    expect(toApiError(res).message).toBe('API token not configured');
  });
});

describe('errorMessage', () => {
  it('handles ApiError, Error and unknown values', () => {
    expect(errorMessage(new ApiError(409, 'Conflict', 'already active'))).toBe('already active');
    expect(errorMessage(new Error('boom'))).toBe('boom');
    expect(errorMessage(42)).toBe('Something went wrong.');
  });
});
