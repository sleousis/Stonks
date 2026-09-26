import type { Routes } from '@angular/router';

/** Guarded by `adminGuard` in app.routes.ts; the API checks the role too. */
export default [
  {
    path: '',
    title: 'Users',
    loadComponent: () => import('./admin-users.page').then((m) => m.AdminUsersPage),
  },
] satisfies Routes;
