import { HttpErrorResponse } from '@angular/common/http';

import type { ProblemDetails } from '../../api/generated/types.gen';

export interface FieldError {
  field: string;
  message: string;
}

/**
 * Every failed API call surfaces as an ApiError. `message` is ready to show
 * to the trader: the API's problem-details `detail` (or `title`), plus a hint
 * for the cases the trader can fix from the console.
 */
/**
 * Stable codes the auth layer puts at the start of `detail`
 * (`mfa_required: ...`), with what the trader should read instead.
 */
const AUTH_CODE_MESSAGES: Readonly<Record<string, string>> = {
  not_authenticated: 'You are signed out. Sign in, or enter an API token in Settings.',
  invalid_credentials: 'That did not match. Try again.',
  mfa_required: 'Finish signing in with your code.',
  step_up_required: 'Confirm it is you with a code from your authenticator app.',
  too_many_attempts: 'Too many tries. Wait a few minutes, then try again.',
  csrf_failed: 'Your session is out of date. Reload the page.',
  forbidden: 'Your role cannot do this.',
};

export type AuthCode = keyof typeof AUTH_CODE_MESSAGES;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    message: string,
    readonly fieldErrors: readonly FieldError[] = [],
    /** The problem's stable code (`mfa_required`, `step_up_required`...), else null. */
    readonly code: string | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  get isAuth(): boolean {
    return this.status === 401;
  }

  get isNetwork(): boolean {
    return this.status === 0;
  }
}

/** Build an ApiError from an error body (usually ProblemDetails) and the HTTP response. */
export function toApiError(body: unknown, response?: unknown): ApiError {
  if (body instanceof ApiError) return body;
  if (body instanceof HttpErrorResponse) return toApiError(body.error, body);

  const status =
    response instanceof HttpErrorResponse
      ? response.status
      : isProblem(body)
        ? body.status
        : typeof (response as { status?: unknown })?.status === 'number'
          ? (response as { status: number }).status
          : 0;

  if (status === 0) {
    return new ApiError(
      0,
      'Network error',
      'Cannot reach the Stonks server. Check your connection and try again. If it keeps failing, tell your admin.',
    );
  }

  const problem = isProblem(body) ? body : null;
  const title = problem?.title ?? statusTitle(status);
  const fieldErrors = (problem?.errors ?? []).map(toFieldError);
  let message = problem?.detail || (typeof body === 'string' && body.trim()) || title;

  const code = authCode(problem?.detail) ?? bodyCode(body);
  if (code && code in AUTH_CODE_MESSAGES) {
    return new ApiError(status, title, AUTH_CODE_MESSAGES[code], fieldErrors, code);
  }

  if (fieldErrors.length) {
    message = `${message}: ${fieldErrors.map((e) => `${e.field} ${e.message}`).join('; ')}`;
  }
  if (status === 401) {
    message = `${message}. Sign in, or enter an API token in Settings.`;
  }
  return new ApiError(status, title, message, fieldErrors, code);
}

/**
 * A stable machine code sent as its own problem field (`"code": "..."`),
 * for servers that send one. Unknown codes are kept on the error but do not
 * change the message.
 */
function bodyCode(body: unknown): string | null {
  const raw = typeof body === 'object' && body !== null ? (body as { code?: unknown }).code : null;
  return typeof raw === 'string' && /^[a-z_]+$/.test(raw) ? raw : null;
}

function authCode(detail: string | null | undefined): AuthCode | null {
  const match = /^([a-z_]+)(?::|$)/.exec(detail ?? '');
  return match && match[1] in AUTH_CODE_MESSAGES ? match[1] : null;
}

/** A user-facing message for anything thrown (ApiError, Error, or unknown). */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof HttpErrorResponse) return toApiError(error).message;
  if (error instanceof Error && error.message) return error.message;
  return 'Something went wrong.';
}

function isProblem(body: unknown): body is ProblemDetails {
  return (
    typeof body === 'object' &&
    body !== null &&
    typeof (body as ProblemDetails).title === 'string' &&
    typeof (body as ProblemDetails).status === 'number'
  );
}

function toFieldError(raw: Record<string, unknown>): FieldError {
  const loc = Array.isArray(raw['loc']) ? (raw['loc'] as unknown[]) : [];
  // Drop the "body"/"query" prefix FastAPI puts first.
  const path = loc.filter((p, i) => !(i === 0 && (p === 'body' || p === 'query' || p === 'path')));
  return {
    field: path.join('.') || 'request',
    message: String(raw['msg'] ?? 'is invalid'),
  };
}

function statusTitle(status: number): string {
  switch (status) {
    case 400:
      return 'Bad request';
    case 401:
      return 'Not authorized';
    case 403:
      return 'Forbidden';
    case 404:
      return 'Not found';
    case 409:
      return 'Conflict';
    case 422:
      return 'Invalid request';
    case 503:
      return 'Service unavailable';
    default:
      return status >= 500 ? 'Server error' : `HTTP ${status}`;
  }
}
