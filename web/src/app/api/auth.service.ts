import { Injectable, inject } from '@angular/core';

import { AuthTokenService } from '../core/auth/auth-token.service';
import { SILENT_HEADERS } from '../core/http/interceptors';
import { allItems, unwrap } from './api-call';
import {
  changePassword,
  checkAuth,
  confirmMfaEnrolment,
  createApiToken,
  getMe,
  listApiTokens,
  listMcpToolsets,
  login,
  logout,
  regenerateRecoveryCodes,
  revokeApiToken,
  startMfaEnrolment,
  verifyMfa,
} from './generated/sdk.gen';
import type { MfaCodeRequest, PasswordChangeRequest, TokenCreateRequest } from './models';

/**
 * Sign-in, second factor, the caller's identity and their API tokens
 * (`/api/auth/*`). Sign-in calls are silent: the login screens explain
 * failures themselves.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly auth = inject(AuthTokenService);

  /**
   * `GET /api/auth/check` with the saved token, even when reads normally go
   * without it: 200 means accepted, 401 rejected, 503 "the API has no token
   * configured". Silent: Settings explains the outcome itself.
   */
  check() {
    return unwrap(checkAuth({ headers: this.withToken() }));
  }

  /** Who the credential (API token, else the session cookie) belongs to. Silent. */
  me() {
    return unwrap(getMe({ headers: this.withToken() }));
  }

  login(email: string, password: string) {
    return unwrap(login({ body: { email, password }, headers: SILENT_HEADERS }));
  }

  startEnrolment() {
    return unwrap(startMfaEnrolment({ headers: SILENT_HEADERS }));
  }

  confirmEnrolment(code: string) {
    return unwrap(confirmMfaEnrolment({ body: { code }, headers: SILENT_HEADERS }));
  }

  /** A TOTP code or a recovery code: finishes sign-in, or refreshes the step-up window. */
  verify(body: MfaCodeRequest) {
    return unwrap(verifyMfa({ body, headers: SILENT_HEADERS }));
  }

  logout() {
    return unwrap(logout({ headers: SILENT_HEADERS }));
  }

  changePassword(body: PasswordChangeRequest) {
    return unwrap(changePassword({ body }));
  }

  regenerateRecoveryCodes() {
    return unwrap(regenerateRecoveryCodes());
  }

  tokens() {
    return allItems((query) => unwrap(listApiTokens({ query })));
  }

  createToken(body: TokenCreateRequest) {
    return unwrap(createApiToken({ body }));
  }

  /** The MCP tool groups a token can be limited to. */
  toolsets() {
    return allItems((query) => unwrap(listMcpToolsets({ query })));
  }

  revokeToken(tokenId: string) {
    return unwrap(revokeApiToken({ path: { token_id: tokenId } }));
  }

  private withToken(): Record<string, string> {
    const token = this.auth.token();
    return token ? { ...SILENT_HEADERS, Authorization: `Bearer ${token}` } : { ...SILENT_HEADERS };
  }
}
