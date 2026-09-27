import type { Routes } from '@angular/router';

import { guestGuard } from '../../core/auth/auth.guards';

export default [
  {
    path: '',
    title: 'Sign in',
    canActivate: [guestGuard],
    loadComponent: () => import('./login.page').then((m) => m.LoginPage),
  },
] satisfies Routes;
