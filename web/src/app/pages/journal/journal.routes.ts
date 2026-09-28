import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Journal',
    loadComponent: () => import('./journal.page').then((m) => m.JournalPage),
  },
  {
    path: 'trades/:tradeId',
    title: 'Trade review',
    loadComponent: () => import('./journal-trade.page').then((m) => m.JournalTradePage),
  },
] satisfies Routes;
