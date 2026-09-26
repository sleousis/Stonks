import { HttpErrorResponse, type HttpInterceptorFn, type HttpRequest } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';

import { AuthTokenService } from '../auth/auth-token.service';
import { ToastService } from '../notify/toast.service';
import { toApiError } from './api-error';

/**
 * Request header that stops the error interceptor from toasting a failure
 * (for background calls with their own fallback). Stripped before sending.
 * Generated SDK calls pass it as `headers: SILENT_HEADERS`.
 */
export const SILENT_HEADER = 'X-Stonks-Silent';
export const SILENT_HEADERS = { [SILENT_HEADER]: '1' } as const;

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/** Same-origin `/api/...` only: the token must never go to another host. */
export function isApiRequest(req: HttpRequest<unknown>): boolean {
  if (req.url.startsWith('/api/') || req.url === '/api') return true;
  try {
    const url = new URL(req.url, window.location.origin);
    return url.origin === window.location.origin && url.pathname.startsWith('/api/');
  } catch {
    return false;
  }
}

/**
 * Every same-origin `/api/` request carries a credential, reads included
 * (the API only leaves health, the probes and sign-in open):
 *
 * - `withCredentials` so the browser sends the session cookie, also when the
 *   console is served from another origin in development;
 * - `Authorization: Bearer <token>` when the tab has an API token (the
 *   server prefers it over the cookie).
 *
 * Other origins get neither.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  if (!isApiRequest(req)) return next(req);
  const token = inject(AuthTokenService).token();
  return next(
    req.clone({
      withCredentials: true,
      ...(token ? { setHeaders: { Authorization: `Bearer ${token}` } } : {}),
    }),
  );
};

/**
 * Toasts the problem-details message of a failed mutating request, and of an
 * auth failure on reads. Read failures otherwise stay with the page, which
 * shows them inline (error state). The error still propagates to the caller.
 */
export const errorInterceptor: HttpInterceptorFn = (req, next) => {
  const toasts = inject(ToastService);
  const silent = req.headers.has(SILENT_HEADER);
  const outgoing = silent ? req.clone({ headers: req.headers.delete(SILENT_HEADER) }) : req;
  return next(outgoing).pipe(
    catchError((err: unknown) => {
      if (err instanceof HttpErrorResponse && !silent) {
        const apiError = toApiError(err);
        if (!SAFE_METHODS.has(req.method) || apiError.isAuth) {
          toasts.error(apiError.message, apiError.title);
        }
      }
      return throwError(() => err);
    }),
  );
};
