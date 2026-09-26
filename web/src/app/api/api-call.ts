import { toApiError } from '../core/http/api-error';

interface SdkResult {
  data?: unknown;
  error?: unknown;
  response?: unknown;
}

/**
 * Await a generated SDK call and return its body, or throw an ApiError with
 * the API's problem-details message. Domain services wrap every SDK call in
 * this so pages only ever see typed data or an ApiError.
 */
export async function unwrap<R extends SdkResult>(
  request: Promise<R>,
): Promise<Exclude<R['data'], undefined>> {
  const result = await request;
  if (result.error !== undefined) {
    throw toApiError(result.error, result.response);
  }
  return result.data as Exclude<R['data'], undefined>;
}
