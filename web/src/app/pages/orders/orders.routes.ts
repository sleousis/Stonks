import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    loadComponent: () => import('./orders.page').then((m) => m.OrdersPage),
    children: [
      {
        path: '',
        title: 'Orders',
        loadComponent: () => import('./orders-list.page').then((m) => m.OrdersListPage),
      },
      {
        path: 'fills',
        title: 'Fills',
        loadComponent: () => import('./fills.page').then((m) => m.FillsPage),
      },
      {
        path: 'ticks',
        title: 'Trading runs',
        loadComponent: () => import('./ticks.page').then((m) => m.TicksPage),
      },
      {
        path: 'ticks/:id',
        title: 'Trading run',
        loadComponent: () => import('./tick-detail.page').then((m) => m.TickDetailPage),
      },
    ],
  },
] satisfies Routes;
