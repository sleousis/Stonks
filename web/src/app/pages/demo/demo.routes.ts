import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Demo portfolio',
    loadComponent: () => import('./demo.page').then((m) => m.DemoPage),
  },
] satisfies Routes;
