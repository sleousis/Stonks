import { type Resource, type Signal, linkedSignal } from '@angular/core';

/**
 * The resource's value, or the last one it had while a new one loads.
 *
 * A server-paged table re-fetches on every page turn. A params change clears
 * the resource's value, so a template that checks `hasValue()` would swap the
 * table for a skeleton on each turn. Render from this signal instead and pass
 * `[busy]="res.isLoading()"`: the old page stays, dimmed, until the next one
 * arrives. An error clears it, so a retry starts from the loading state.
 *
 *   protected readonly page = keepLatest(this.orders);
 */
export function keepLatest<T>(res: Resource<T>): Signal<T | undefined> {
  return linkedSignal<{ value: T | undefined; failed: boolean }, T | undefined>({
    source: () => ({
      value: res.hasValue() ? res.value() : undefined,
      failed: res.error() != null,
    }),
    computation: ({ value, failed }, prev) => {
      if (failed) return undefined;
      return value !== undefined ? value : prev?.value;
    },
  });
}
