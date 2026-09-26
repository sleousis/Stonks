import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Trade costs',
    loadComponent: () => import('./trades.page').then((m) => m.TradesPage),
  },
  {
    path: 'orders/:clientId',
    title: 'Order costs',
    loadComponent: () => import('./trade-order.page').then((m) => m.TradeOrderPage),
  },
] satisfies Routes;
