import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Notifications',
    loadComponent: () => import('./notifications.page').then((m) => m.NotificationsPage),
  },
  {
    path: 'price-alerts',
    title: 'Price alerts',
    loadComponent: () => import('./price-alerts.page').then((m) => m.PriceAlertsPage),
  },
  {
    path: 'settings',
    title: 'Alert settings',
    loadComponent: () => import('./alert-settings.page').then((m) => m.AlertSettingsPage),
  },
] satisfies Routes;
