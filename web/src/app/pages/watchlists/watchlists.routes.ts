import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Watchlists',
    loadComponent: () => import('./watchlists.page').then((m) => m.WatchlistsPage),
  },
] satisfies Routes;
