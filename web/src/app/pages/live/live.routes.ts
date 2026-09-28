import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Live engine',
    loadComponent: () => import('./live.page').then((m) => m.LivePage),
  },
] satisfies Routes;
