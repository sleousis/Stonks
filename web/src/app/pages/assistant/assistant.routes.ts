import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Assistant',
    loadComponent: () => import('./assistant.page').then((m) => m.AssistantPage),
  },
] satisfies Routes;
