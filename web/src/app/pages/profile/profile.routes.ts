import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Profile',
    loadComponent: () => import('./profile.page').then((m) => m.ProfilePage),
  },
] satisfies Routes;
