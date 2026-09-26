import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Notifications',
    loadComponent: () => import('./notifications.page').then((m) => m.NotificationsPage),
  },
] satisfies Routes;
