import { ApiError } from '../../core/http/api-error';

export type TokenCheckState = 'valid' | 'invalid' | 'unverified';

export interface TokenCheck {
  state: TokenCheckState;
  message: string;
}

export const TOKEN_ACCEPTED: TokenCheck = {
  state: 'valid',
  message: 'The API accepted this token. Actions are enabled.',
};

export const NO_TOKEN: TokenCheck = {
  state: 'unverified',
  message: 'Save a token first, then test it.',
};

/**
 * Reads why `GET /api/auth/check` failed (a 200 is `TOKEN_ACCEPTED`).
 * Never includes the token in the message.
 */
export function tokenCheckFromError(err: unknown): TokenCheck {
  const status = err instanceof ApiError ? err.status : -1;
  switch (status) {
    case 401:
    case 403:
      return {
        state: 'invalid',
        message:
          'The API rejected this token. It may be revoked or mistyped. Create a new one on your Profile page.',
      };
    case 503:
      return {
        state: 'unverified',
        message: "The server can't check tokens right now. Ask your admin, or sign in instead.",
      };
    case 0:
      return {
        state: 'unverified',
        message:
          "Cannot reach the server, so the token can't be checked yet. Try again in a moment.",
      };
    default:
      return {
        state: 'unverified',
        message: `Could not verify the token: ${err instanceof Error ? err.message : 'unexpected response'}.`,
      };
  }
}
