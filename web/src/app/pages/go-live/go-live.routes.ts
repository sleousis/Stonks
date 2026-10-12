import type { Routes } from '@angular/router';

import { adminGuard } from '../../core/auth/auth.guards';
import { oldReviewLinkGuard } from './go-live.guards';

export default [
  {
    path: '',
    title: 'Strategy review',
    // An old ?strategy= link goes to the Review tab first, so a trader's
    // link still works. The page itself is the admin's approval queue, and
    // the API checks every approval too.
    canActivate: [oldReviewLinkGuard, adminGuard],
    loadComponent: () => import('./go-live.page').then((m) => m.GoLivePage),
  },
] satisfies Routes;
