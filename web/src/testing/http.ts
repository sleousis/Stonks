import type { HttpTestingController, TestRequest } from '@angular/common/http/testing';

/** A paged list body (`{ items, total, limit, offset }`) holding every item. */
export function page<T>(items: readonly T[]) {
  return { items: [...items], total: items.length, limit: 500, offset: 0 };
}

/** Let pending promises and zero-delay timers run. */
export function tick(ms = 0): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Wait until exactly one request matching `path` (URL without the query
 * string) and `method` is pending, then return it. Generated SDK calls are issued
 * after a few microtasks, so `expectOne` right after the call would miss them.
 */
export async function nextRequest(
  controller: HttpTestingController,
  path: string,
  method = 'GET',
  timeoutMs = 1000,
): Promise<TestRequest> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = controller.match(
      (req) => req.method === method && req.url.split('?')[0] === path,
    );
    if (found.length > 1) {
      // match() consumes every hit; returning one would silently drop the rest.
      throw new Error(
        `${found.length} pending ${method} ${path} requests; flush them with controller.match()`,
      );
    }
    if (found.length) return found[0];
    if (Date.now() > deadline) throw new Error(`no ${method} ${path} request was made`);
    await tick(1);
  }
}
