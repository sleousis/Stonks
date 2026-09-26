import type { Routes } from '@angular/router';

export default [
  { path: '', pathMatch: 'full', redirectTo: 'glossary' },
  {
    path: 'glossary',
    title: 'Glossary',
    loadComponent: () => import('./glossary.page').then((m) => m.GlossaryPage),
  },
] satisfies Routes;
