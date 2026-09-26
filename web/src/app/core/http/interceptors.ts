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

function isStreamToken(req: HttpRequest<unknown>): boolean {
  return /\/api\/jobs\/[^/]+\/stream-token$/.test(req.url.split('?')[0]);
}

/**
 * Attaches `Authorization: Bearer <token>` to mutating API requests and to
 * stream-token calls; to reads too when the trader turned on "send token on
 * reads" in Settings.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthTokenService);
  const token = auth.token();
  if (!token || !isApiRequest(req)) return next(req);
  const needsToken = !SAFE_METHODS.has(req.method) || isStreamToken(req) || auth.sendOnReads();
  if (!needsToken) return next(req);
  return next(req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }));
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
