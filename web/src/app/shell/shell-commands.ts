import { DestroyRef, inject } from '@angular/core';
import { Router } from '@angular/router';

import { TicksService } from '../api/ticks.service';
import { type PaletteCommand, CommandRegistry } from '../core/commands/command-registry';
import { type KeySequence, ShortcutsService } from '../core/commands/shortcuts.service';
import { ConfirmService } from '../core/confirm/confirm.service';
import { GLOSSARY_PATH } from '../core/help/glossary';
import { JobsService } from '../core/jobs/jobs.service';
import { ToastService } from '../core/notify/toast.service';
import { ThemeService } from '../core/theme/theme.service';
import { NAV_ITEMS } from './nav-items';

/** Pages below the top level that traders jump to directly. */
const SUB_PAGES: readonly { path: string; label: string; keywords: string[] }[] = [
  { path: '/orders/ticks', label: 'Ticks', keywords: ['runs', 'history'] },
  { path: '/orders/fills', label: 'Fills', keywords: ['executions', 'trades'] },
];

export const DRY_RUN_TICK = 'action.dry-run-tick';
export const NEW_BACKTEST = 'action.new-backtest';

/** Action shortcuts: `n` then a key. Page shortcuts come from NAV_ITEMS (`g` then a key). */
const ACTION_SEQUENCES: readonly KeySequence[] = [
  { prefix: 'n', key: 'b', label: 'New backtest', commandId: NEW_BACKTEST },
  { prefix: 'n', key: 't', label: 'Try a dry run', commandId: DRY_RUN_TICK },
];

/**
 * Registers the palette's pages and global actions and the keyboard
 * sequences for them. Called once from the shell's constructor.
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
  const destroyRef = inject(DestroyRef);

  const go = (path: string) => () => void router.navigateByUrl(path);

  const pages: PaletteCommand[] = [
    ...NAV_ITEMS.map((item) => ({
      id: `page.${item.path}`,
      label: item.label,
      group: 'Pages' as const,
      keywords: [item.group],
      hint: `g ${item.key}`,
      run: go(item.path),
    })),
    ...SUB_PAGES.map((p) => ({
      id: `page.${p.path}`,
      label: p.label,
      group: 'Pages' as const,
      keywords: p.keywords,
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
      keywords: ['tick', 'dry run', 'simulate', 'orders'],
      hint: 'n t',
      run: dryRunTick,
    },
    {
      id: NEW_BACKTEST,
      label: 'New backtest',
      group: 'Actions',
      keywords: ['lab', 'test', 'simulate'],
      hint: 'n b',
      run: go('/lab'),
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
      keywords: ['help', 'metrics', 'definitions', 'sharpe'],
      run: () => void router.navigateByUrl(GLOSSARY_PATH),
    },
  ];

  registry.register([...pages, ...actions], destroyRef);
  shortcuts.setSequences([
    ...NAV_ITEMS.map((i) => ({ prefix: 'g' as const, key: i.key, label: i.label, path: i.path })),
    ...ACTION_SEQUENCES,
  ]);
}
