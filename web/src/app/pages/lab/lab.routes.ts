import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Lab',
    loadComponent: () => import('./lab.page').then((m) => m.LabPage),
  },
] satisfies Routes;
