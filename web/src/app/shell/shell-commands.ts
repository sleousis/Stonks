import { DestroyRef, inject } from '@angular/core';
import { Router } from '@angular/router';

import { TicksService } from '../api/ticks.service';
import type { Permission } from '../core/auth/permissions';
import { SessionService } from '../core/auth/session.service';
import { FeatureFlagsService } from '../core/features/feature-flags.service';
import { type PaletteCommand, CommandRegistry } from '../core/commands/command-registry';
import { type KeySequence, ShortcutsService } from '../core/commands/shortcuts.service';
import { ConfirmService } from '../core/confirm/confirm.service';
import { StopTradingService } from '../core/halts/stop-trading.service';
import { GLOSSARY_PATH } from '../core/help/glossary';
import { JobsService } from '../core/jobs/jobs.service';
import { ToastService } from '../core/notify/toast.service';
import { ThemeService } from '../core/theme/theme.service';
import { type NavItem, NAV_ITEMS, navItemVisible, navViewer } from './nav-items';

/**
 * Pages below the top level that traders jump to directly, named as their
 * tabs name them (UX-41).
 */
const SUB_PAGES: readonly (Omit<NavItem, 'group'> & { keywords: readonly string[] })[] = [
  { path: '/orders/ticks', label: 'Trading runs', keywords: ['runs', 'history', 'orders'] },
  { path: '/orders/fills', label: 'Fills', keywords: ['executions', 'trades'] },
];

export const DRY_RUN_TICK = 'action.dry-run-tick';
export const NEW_BACKTEST = 'action.new-backtest';

/**
 * Registers the palette's pages and global actions and the keyboard
 * sequences for them. Called once from the shell's constructor. Words and
 * scope match the nav: pages and actions the user may not use are left out,
 * and that follows the signed-in user as it changes.
 */
export function registerShellCommands(): void {
  const registry = inject(CommandRegistry);
  const shortcuts = inject(ShortcutsService);
  const router = inject(Router);
  const ticks = inject(TicksService);
  const jobs = inject(JobsService);
  const confirm = inject(ConfirmService);
  const toasts = inject(ToastService);
  const theme = inject(ThemeService);
  const viewer = navViewer(inject(SessionService), inject(FeatureFlagsService));
  const stopTrading = inject(StopTradingService);
  const destroyRef = inject(DestroyRef);

  const go = (path: string) => () => void router.navigateByUrl(path);
  const allowedTo = (permission: Permission) => () => viewer.can(permission);
  const shows = (item: NavItem) => () => navItemVisible(item, viewer);

  const pages: PaletteCommand[] = [
    ...NAV_ITEMS.map((item) => ({
      id: `page.${item.path}`,
      label: item.label,
      group: 'Pages' as const,
      keywords: [...(item.keywords ?? [])],
      hint: item.key ? `g ${item.key}` : undefined,
      visible: shows(item),
      run: go(item.path),
    })),
    ...SUB_PAGES.map((p) => ({
      id: `page.${p.path}`,
      label: p.label,
      group: 'Pages' as const,
      keywords: p.keywords,
      hint: p.key ? `g ${p.key}` : undefined,
      run: go(p.path),
    })),
  ];

  async function dryRunTick(): Promise<void> {
    const ok = await confirm.confirm({
      title: 'Try a dry run?',
      message:
        'Strategies decide and orders are sized, but nothing is sent to the broker and no fills are recorded.',
      confirmLabel: 'Run dry run',
    });
    if (!ok) return;
    try {
      const job = await ticks.start({ dry_run: true, as_of: null, tickers: null });
      toasts.info('Follow it on the Trading runs page.', 'Dry run started');
      const last = await jobs.track(job.id, destroyRef).finished;
      ticks.announceFinished();
      if (last?.status === 'succeeded')
        toasts.success('See the sized orders on the Trading runs page.', 'Dry run finished');
      else if (last?.status === 'failed')
        toasts.error(last.error ?? 'The dry run failed.', 'Dry run failed');
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  const actions: PaletteCommand[] = [
    {
      id: DRY_RUN_TICK,
      label: 'Try a dry run',
      group: 'Actions',
      keywords: ['trading run', 'dry run', 'practice', 'orders'],
      hint: 'n t',
      visible: allowedTo('operations.run'),
      run: dryRunTick,
    },
    {
      id: NEW_BACKTEST,
      label: 'New backtest',
      group: 'Actions',
      keywords: ['lab', 'test', 'simulate'],
      hint: 'n b',
      visible: allowedTo('lab.run'),
      run: go('/lab'),
    },
    {
      id: 'action.stop-trading',
      label: 'Stop trading',
      group: 'Actions',
      keywords: ['kill switch', 'halt', 'emergency'],
      visible: allowedTo('killswitch.user'),
      run: () => stopTrading.open.set(true),
    },
    {
      id: 'action.theme',
      label: 'Switch light or dark theme',
      group: 'Actions',
      keywords: ['theme', 'dark', 'light', 'appearance'],
      run: () => theme.toggle(),
    },
    {
      id: 'action.shortcuts',
      label: 'Show keyboard shortcuts',
      group: 'Actions',
      keywords: ['keys', 'help', 'hotkeys'],
      hint: '?',
      run: () => shortcuts.openHelp(),
    },
    {
      id: 'action.glossary',
      label: 'Open the glossary',
      group: 'Actions',
      keywords: ['help', 'metrics', 'definitions', 'words'],
      run: () => void router.navigateByUrl(GLOSSARY_PATH),
    },
  ];

  const pageSequences: KeySequence[] = [...NAV_ITEMS, ...SUB_PAGES].flatMap((item) =>
    item.key
      ? [
          {
            prefix: 'g' as const,
            key: item.key,
            label: item.label,
            path: item.path,
            visible: 'group' in item ? shows(item) : undefined,
          },
        ]
      : [],
  );

  registry.register([...pages, ...actions], destroyRef);
  shortcuts.setSequences([
    ...pageSequences,
    {
      prefix: 'n',
      key: 'b',
      label: 'New backtest',
      commandId: NEW_BACKTEST,
      visible: allowedTo('lab.run'),
    },
    {
      prefix: 'n',
      key: 't',
      label: 'Try a dry run',
      commandId: DRY_RUN_TICK,
      visible: allowedTo('operations.run'),
    },
  ]);
}
