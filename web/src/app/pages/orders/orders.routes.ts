import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Orders',
    loadComponent: () => import('./orders.page').then((m) => m.OrdersPage),
  },
] satisfies Routes;
