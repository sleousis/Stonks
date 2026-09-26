import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Data',
    loadComponent: () => import('./data.page').then((m) => m.DataPage),
  },
] satisfies Routes;
