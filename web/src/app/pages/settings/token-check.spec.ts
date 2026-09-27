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

  it('explains 503 as a server that cannot check tokens yet', () => {
    const check = tokenCheckFromError(new ApiError(503, 'Unavailable', 'no token'));
    expect(check.state).toBe('unverified');
    expect(check.message).toContain("can't check tokens");
  });

  it('explains a network failure', () => {
    const check = tokenCheckFromError(new ApiError(0, 'Network error', 'down'));
    expect(check.state).toBe('unverified');
    expect(check.message).toContain('Cannot reach the server');
  });

  it('never sends the trader to a terminal or a config file', () => {
    for (const status of [0, 401, 403, 503]) {
      const { message } = tokenCheckFromError(new ApiError(status, 'x', 'y'));
      expect(message).not.toMatch(/stonks |STONKS_|`/);
    }
  });

  it('falls back to the error message for anything else', () => {
    expect(tokenCheckFromError(new ApiError(500, 'Server error', 'boom')).message).toBe(
      'Could not verify the token: boom.',
    );
  });
});
