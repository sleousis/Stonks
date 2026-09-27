import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Universes',
    loadComponent: () => import('./universes.page').then((m) => m.UniversesPage),
  },
  {
    path: ':id',
    title: 'Universe',
    loadComponent: () => import('./universe-detail.page').then((m) => m.UniverseDetailPage),
  },
] satisfies Routes;
