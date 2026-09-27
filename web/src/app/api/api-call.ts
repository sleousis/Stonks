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

/** The largest page the API serves (`[api].max_page_size`). */
export const MAX_PAGE_SIZE = 500;

/**
 * Every item of a paged list route (`{ items, total, limit, offset }`),
 * read page after page. For lists a page shows whole (halts, tokens,
 * universes); tables with their own pager ask for one page instead.
 */
export async function allItems<T>(
  fetchPage: (query: { limit: number; offset: number }) => Promise<{ items: T[]; total: number }>,
): Promise<T[]> {
  const items: T[] = [];
  for (;;) {
    const page = await fetchPage({ limit: MAX_PAGE_SIZE, offset: items.length });
    items.push(...page.items);
    if (page.items.length === 0 || items.length >= page.total) return items;
  }
}
