import { HttpErrorResponse, type HttpInterceptorFn, type HttpRequest } from '@angular/common/http';
import { Injector, inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, from, switchMap, throwError } from 'rxjs';

import { toApiError } from '../http/api-error';
import { isApiRequest, quietError } from '../http/interceptors';
import { AuthTokenService } from './auth-token.service';
import { SessionService } from './session.service';
import { StepUpService } from './step-up.service';

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
export const CSRF_HEADER = 'X-CSRF-Token';

/** Sign-in routes explain their own failures; never redirect from them. */
const SELF_HANDLED = /^\/api\/auth\/(login|logout|me|check|mfa\/)/;

function pathOf(url: string): string {
  try {
    return new URL(url, window.location.origin).pathname;
  } catch {
    return url.split('?')[0];
  }
}

/**
 * The browser-session side of every API call:
 *
 * - adds `X-CSRF-Token` to unsafe same-origin `/api/` requests that ride on
 *   the session cookie (not when an API token is sent instead);
 * - 401 `mfa_required`: the second factor is missing, go to the code screen;
 * - 401 on a session that was signed in: it expired, go to sign-in;
 * - 401 with the tab's API token: it was revoked, forget it and go to sign-in;
 * - 403 `step_up_required`: ask for a fresh code, then retry once (a
 *   cancelled prompt fails quietly, without an error toast).
 *
 * Registered after the error interceptor, so it sees failures first and a
 * step-up that succeeds is never toasted.
 */
export const sessionInterceptor: HttpInterceptorFn = (req, next) => {
  if (!isApiRequest(req)) return next(req);
  const session = inject(SessionService);
  const injector = inject(Injector);
  const bearer = req.headers.has('Authorization');

  const withCsrf = (r: HttpRequest<unknown>): HttpRequest<unknown> => {
    if (SAFE_METHODS.has(r.method) || bearer) return r;
    const token = session.csrfToken();
    return token ? r.clone({ setHeaders: { [CSRF_HEADER]: token } }) : r;
  };

  return next(withCsrf(req)).pipe(
    catchError((err: unknown) => {
      if (!(err instanceof HttpErrorResponse) || SELF_HANDLED.test(pathOf(req.url))) {
        return throwError(() => err);
      }

      if (bearer) {
        // The tab's API token was revoked or expired (UX-39): forget it and sign in.
        const tokens = injector.get(AuthTokenService);
        const tabToken = tokens.token();
        if (
          err.status === 401 &&
          tabToken &&
          req.headers.get('Authorization') === `Bearer ${tabToken}`
        ) {
          tokens.clear();
          session.markSignedOut();
          const router = injector.get(Router);
          void router.navigate(['/login'], { queryParams: { next: router.url } });
        }
        return throwError(() => err);
      }

      const apiError = toApiError(err);
      const code = apiError.code;

      if (err.status === 403 && code === 'step_up_required' && !SAFE_METHODS.has(req.method)) {
        return from(injector.get(StepUpService).prompt()).pipe(
          // Cancelled: the trader knows, so no error toast on top (UX-38).
          switchMap((ok) => (ok ? next(withCsrf(req)) : throwError(() => quietError(err)))),
        );
      }

      if (err.status === 401) {
        const router = injector.get(Router);
        const here = router.url;
        if (code === 'mfa_required') {
          session.markMfaPending(apiError.nextStep);
          void router.navigate(['/login'], { queryParams: { step: 'code', next: here } });
        } else if (session.status() === 'signed-in') {
          session.markSignedOut();
          void router.navigate(['/login'], { queryParams: { next: here } });
        }
      }
      return throwError(() => err);
    }),
  );
};
