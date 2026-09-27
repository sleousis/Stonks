import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Options research',
    loadComponent: () => import('./options.page').then((m) => m.OptionsPage),
  },
] satisfies Routes;
