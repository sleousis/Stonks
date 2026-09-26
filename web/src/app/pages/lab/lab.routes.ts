import type { Routes } from '@angular/router';

export default [
  {
    path: '',
    title: 'Lab',
    loadComponent: () => import('./lab.page').then((m) => m.LabPage),
  },
  {
    path: 'sweeps',
    title: 'Lab sweep',
    loadComponent: () => import('./sweeps.page').then((m) => m.SweepsPage),
  },
  {
    path: 'signal-ic',
    title: 'Lab signal IC',
    loadComponent: () => import('./signal-ic.page').then((m) => m.SignalIcPage),
  },
] satisfies Routes;
