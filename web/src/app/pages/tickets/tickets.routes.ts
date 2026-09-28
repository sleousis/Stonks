import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Approvals',
    loadComponent: () => import('./tickets.page').then((m) => m.TicketsPage),
  },
] satisfies Routes;
