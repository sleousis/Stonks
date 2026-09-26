import { Injectable, inject } from '@angular/core';

import { AuthTokenService } from '../core/auth/auth-token.service';
import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import { checkAuth } from './generated/sdk.gen';

/** The API's bearer-token check. */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly auth = inject(AuthTokenService);

  /**
   * `GET /api/auth/check` with the saved token, even when reads normally go
   * without it: 200 means accepted, 401 rejected, 503 "the API has no token
   * configured". Silent: Settings explains the outcome itself.
   */
  check() {
    const token = this.auth.token();
    return unwrap(
      checkAuth({
        headers: token
          ? { ...SILENT_HEADERS, Authorization: `Bearer ${token}` }
          : { ...SILENT_HEADERS },
      }),
    );
  }
}
