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

  it('adds a Settings hint to 401s', () => {
    const body = { title: 'Unauthorized', status: 401, detail: 'missing or invalid bearer token' };
    const err = toApiError(body, new HttpErrorResponse({ status: 401, error: body }));
    expect(err.isAuth).toBe(true);
    expect(err.message).toBe('missing or invalid bearer token. Enter the API token in Settings.');
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
