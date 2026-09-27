import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Calendar',
    loadComponent: () => import('./calendar.page').then((m) => m.CalendarPage),
  },
] satisfies Routes;
