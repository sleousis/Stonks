import { ApiError } from '../../core/http/api-error';
import { TOKEN_ACCEPTED, tokenCheckFromError } from './token-check';

describe('tokenCheckFromError', () => {
  it('never reads an error as an accepted token', () => {
    for (const status of [0, 400, 404, 500, 503]) {
      expect(tokenCheckFromError(new ApiError(status, 'x', 'y'))).not.toBe(TOKEN_ACCEPTED);
    }
  });

  it('reads 401 as a rejected token', () => {
    expect(tokenCheckFromError(new ApiError(401, 'Unauthorized', 'bad')).state).toBe('invalid');
  });

  it('explains 503 as a server without a token, not verifiable yet', () => {
    const check = tokenCheckFromError(new ApiError(503, 'Unavailable', 'no token'));
    expect(check.state).toBe('unverified');
    expect(check.message).toContain("can't be verified");
  });

  it('explains a network failure', () => {
    const check = tokenCheckFromError(new ApiError(0, 'Network error', 'down'));
    expect(check.state).toBe('unverified');
    expect(check.message).toContain('Cannot reach the API');
  });

  it('falls back to the error message for anything else', () => {
    expect(tokenCheckFromError(new ApiError(500, 'Server error', 'boom')).message).toBe(
      'Could not verify the token: boom.',
    );
  });
});
