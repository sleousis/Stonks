import type { Routes } from '@angular/router';

import type { DraftPage } from './draft.page';

export default [
  {
    path: '',
    title: 'Studio',
    loadComponent: () => import('./studio.page').then((m) => m.StudioPage),
  },
  {
    path: ':id',
    title: 'Studio draft',
    loadComponent: () => import('./draft.page').then((m) => m.DraftPage),
    canDeactivate: [(page: DraftPage) => page.canLeave()],
  },
] satisfies Routes;
