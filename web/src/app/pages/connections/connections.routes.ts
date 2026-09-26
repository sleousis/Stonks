import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Broker connections',
    loadComponent: () => import('./connections.page').then((m) => m.ConnectionsPage),
  },
] satisfies Routes;
