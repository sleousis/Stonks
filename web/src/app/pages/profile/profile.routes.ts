import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Profile',
    loadComponent: () => import('./profile.page').then((m) => m.ProfilePage),
  },
  {
    path: 'live/:id',
    title: 'Live settings',
    loadComponent: () => import('./live-settings.page').then((m) => m.LiveSettingsPage),
  },
] satisfies Routes;
