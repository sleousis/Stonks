import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Broker connections',
    loadComponent: () => import('./connections.page').then((m) => m.ConnectionsPage),
  },
  {
    // Before ':id', so the portal's return address is not read as an id.
    path: 'callback',
    title: 'Finishing your connection',
    loadComponent: () => import('./connection-callback.page').then((m) => m.ConnectionCallbackPage),
  },
  {
    // 23.17: CSV statements for brokers without an API. Before ':id' too.
    path: 'import',
    title: 'Import a CSV statement',
    loadComponent: () => import('./statement-import.page').then((m) => m.StatementImportPage),
  },
  {
    path: ':id',
    title: 'Broker connection',
    loadComponent: () => import('./connection-detail.page').then((m) => m.ConnectionDetailPage),
  },
] satisfies Routes;
