import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Settings',
    loadComponent: () => import('./settings.page').then((m) => m.SettingsPage),
  },
] satisfies Routes;
