import type { Routes } from '@angular/router';

export default [
  { path: '', pathMatch: 'full', redirectTo: 'halts' },
  {
    path: 'halts',
    title: 'Halts',
    loadComponent: () => import('./halts.page').then((m) => m.HaltsPage),
  },
] satisfies Routes;
