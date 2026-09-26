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
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    message: string,
    readonly fieldErrors: readonly FieldError[] = [],
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
      'Cannot reach the Stonks API. Check that `stonks serve` is running.',
    );
  }

  const problem = isProblem(body) ? body : null;
  const title = problem?.title ?? statusTitle(status);
  const fieldErrors = (problem?.errors ?? []).map(toFieldError);
  let message = problem?.detail || (typeof body === 'string' && body.trim()) || title;

  if (fieldErrors.length) {
    message = `${message}: ${fieldErrors.map((e) => `${e.field} ${e.message}`).join('; ')}`;
  }
  if (status === 401) {
    message = `${message}. Enter the API token in Settings.`;
  }
  return new ApiError(status, title, message, fieldErrors);
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
