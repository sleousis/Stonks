import { inject } from '@angular/core';
import { type CanActivateFn, Router } from '@angular/router';

/**
 * Old `/go-live?strategy=<id>` links open that strategy's Review tab, for
 * anyone who may read it. Runs before `adminGuard` on `/go-live`, so a
 * trader's old link still lands on the strategy page, not on No access.
 */
export const oldReviewLinkGuard: CanActivateFn = (route) => {
  const id = route.queryParamMap.get('strategy');
  if (!id) return true;
  return inject(Router).createUrlTree(['/strategies', id], { queryParams: { tab: 'review' } });
};
