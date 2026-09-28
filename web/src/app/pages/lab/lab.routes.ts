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
  {
    path: 'ledger',
    title: 'Lab trial ledger',
    loadComponent: () => import('./ledger.page').then((m) => m.LedgerPage),
  },
  {
    path: 'ledger/:runId',
    title: 'Lab run in the ledger',
    loadComponent: () => import('./ledger-run.page').then((m) => m.LedgerRunPage),
  },
  {
    path: 'factors',
    title: 'Lab factors',
    loadComponent: () => import('./factors/factors.page').then((m) => m.FactorsPage),
  },
  {
    path: 'factors/:factorId',
    title: 'Lab factor',
    loadComponent: () => import('./factors/factor-detail.page').then((m) => m.FactorDetailPage),
  },
  {
    path: 'research',
    title: 'Lab research sessions',
    loadComponent: () => import('./research/research.page').then((m) => m.ResearchPage),
  },
  {
    path: 'research/:sessionId',
    title: 'Lab research session',
    loadComponent: () =>
      import('./research/research-session.page').then((m) => m.ResearchSessionPage),
  },
] satisfies Routes;
