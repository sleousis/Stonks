import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Studio',
    loadComponent: () => import('./studio.page').then((m) => m.StudioPage),
  },
] satisfies Routes;
