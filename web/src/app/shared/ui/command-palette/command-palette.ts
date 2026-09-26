import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  type ElementRef,
  computed,
  effect,
  inject,
  linkedSignal,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';

import type { InstrumentView, Job, StrategySummary } from '../../../api/models';
import { SearchService } from '../../../api/search.service';
import { CommandRegistry, commandScore } from '../../../core/commands/command-registry';
import { ShortcutsService } from '../../../core/commands/shortcuts.service';
import { formatAgo } from '../../../core/format/format';

export interface PaletteItem {
  id: string;
  group: string;
  label: string;
  detail?: string;
  hint?: string;
  run: () => void | Promise<void>;
}

interface Section {
  group: string;
  items: (PaletteItem & { index: number })[];
}

const DEBOUNCE_MS = 150;
const PER_GROUP = 6;

/** Where a job's results are shown, by job kind. */
export function jobPath(kind: string): string {
  if (kind.includes('tick')) return '/orders/ticks';
  if (kind.includes('backtest') || kind.includes('lab')) return '/lab';
  if (kind.includes('ingest')) return '/data';
  return '/';
}

export function jobLabel(kind: string): string {
  const labels: Record<string, string> = {
    backtest: 'Backtest',
    lab_run: 'Lab run',
    tick: 'Tick',
    ingest: 'Ingest',
  };
  return labels[kind] ?? kind.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function jobStrategy(job: Job): string | null {
  const ref = job.params['strategy'] as { strategy_id?: string; class_path?: string } | undefined;
  return ref?.strategy_id ?? ref?.class_path?.split(/[:.]/).at(-1) ?? null;
}

/**
 * Ctrl+K / Cmd+K search across pages, actions, strategies, tickers and
 * recent jobs. A modal <dialog> (focus stays inside; Escape closes) with the
 * ARIA combobox pattern: the input owns a listbox and points at the active
 * option with aria-activedescendant, so focus never leaves the input.
 * Mounted once, in the shell; opened through ShortcutsService.
 */
@Component({
  selector: 'app-command-palette',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './command-palette.html',
  styleUrl: './command-palette.scss',
})
export class CommandPalette {
  private readonly shortcuts = inject(ShortcutsService);
  private readonly registry = inject(CommandRegistry);
  private readonly search = inject(SearchService);
  private readonly router = inject(Router);
  private readonly doc = inject(DOCUMENT);

  private readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>('dialog');
  private readonly input = viewChild.required<ElementRef<HTMLInputElement>>('input');

  protected readonly open = this.shortcuts.paletteOpen;
  protected readonly query = signal('');
  private readonly debounced = signal('');
  private returnFocus: HTMLElement | null = null;

  protected readonly strategies = resource({
    params: () => (this.open() ? {} : undefined),
    loader: () => this.search.strategies(),
  });
  protected readonly jobs = resource({
    params: () => (this.open() ? {} : undefined),
    loader: () => this.search.recentJobs(),
  });
  protected readonly tickers = resource({
    params: () => {
      const q = this.debounced().trim();
      return this.open() && q.length > 0 ? { q } : undefined;
    },
    loader: ({ params }) => this.search.instruments(params.q),
  });

  protected readonly sections = computed<Section[]>(() => {
    const q = this.query().trim();
    const groups: [string, PaletteItem[]][] = q
      ? [
          ['Pages', this.commandItems('Pages', q)],
          ['Actions', this.commandItems('Actions', q)],
          ['Strategies', this.strategyItems(q)],
          ['Tickers', this.tickerItems()],
          ['Recent jobs', this.jobItems(q)],
        ]
      : [
          ['Actions', this.commandItems('Actions', '')],
          ['Recent jobs', this.jobItems('')],
          ['Pages', this.commandItems('Pages', '')],
        ];
    let index = 0;
    return groups
      .filter(([, items]) => items.length > 0)
      .map(([group, items]) => ({
        group,
        items: items.slice(0, PER_GROUP).map((item) => ({ ...item, index: index++ })),
      }));
  });

  protected readonly flat = computed(() => this.sections().flatMap((s) => s.items));
  protected readonly active = linkedSignal({ source: this.flat, computation: () => 0 });
  protected readonly activeId = computed(() => {
    const item = this.flat()[this.active()];
    return item ? this.optionId(item.index) : null;
  });

  /** One line under the input: what is still loading or failed. */
  protected readonly status = computed(() => {
    const q = this.query().trim();
    if (q && (this.tickers.isLoading() || this.debounced().trim() !== q)) return 'Searching…';
    const failed = [
      this.strategies.error() ? 'strategies' : null,
      q && this.tickers.error() ? 'tickers' : null,
      this.jobs.error() ? 'jobs' : null,
    ].filter(Boolean);
    if (failed.length) return `Could not load ${failed.join(' and ')}. Check the API on Health.`;
    return '';
  });

  protected readonly announcement = computed(() => {
    const n = this.flat().length;
    if (!this.query().trim()) return '';
    return n === 0 ? 'No results' : n === 1 ? '1 result' : `${n} results`;
  });

  constructor() {
    effect(() => {
      const el = this.dialog().nativeElement;
      if (this.open()) {
        if (!el.open) {
          const focused = this.doc.activeElement;
          this.returnFocus = focused instanceof HTMLElement ? focused : null;
          this.query.set('');
          this.debounced.set('');
          el.showModal?.();
          if (!el.showModal) el.setAttribute('open', '');
          this.input().nativeElement.focus();
        }
      } else if (el.open) {
        el.close?.();
        if (!el.close) el.removeAttribute('open');
      }
    });

    effect((onCleanup) => {
      const q = this.query();
      const timer = setTimeout(() => this.debounced.set(q), DEBOUNCE_MS);
      onCleanup(() => clearTimeout(timer));
    });
  }

  protected optionId(index: number): string {
    return `palette-option-${index}`;
  }

  protected onInput(value: string): void {
    this.query.set(value);
  }

  protected onKeydown(event: KeyboardEvent): void {
    const count = this.flat().length;
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (count) this.active.set((this.active() + 1) % count);
        this.scrollActive();
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (count) this.active.set((this.active() - 1 + count) % count);
        this.scrollActive();
        break;
      case 'PageDown':
        event.preventDefault();
        if (count) this.active.set(count - 1);
        this.scrollActive();
        break;
      case 'PageUp':
        event.preventDefault();
        this.active.set(0);
        this.scrollActive();
        break;
      case 'Enter': {
        event.preventDefault();
        const item = this.flat()[this.active()];
        if (item) void this.choose(item);
        break;
      }
    }
  }

  protected async choose(item: PaletteItem): Promise<void> {
    this.close();
    await item.run();
  }

  protected close(): void {
    this.open.set(false);
  }

  /** Native close (Escape, or close()): sync state and put focus back. */
  protected onClosed(): void {
    this.open.set(false);
    const target = this.returnFocus;
    this.returnFocus = null;
    if (target?.isConnected && this.doc.activeElement === this.doc.body) target.focus();
  }

  /** Click on the backdrop (the dialog element itself) closes. */
  protected onDialogClick(event: MouseEvent): void {
    if (event.target === this.dialog().nativeElement) this.close();
  }

  private scrollActive(): void {
    const id = this.activeId();
    if (!id) return;
    this.doc.getElementById(id)?.scrollIntoView?.({ block: 'nearest' });
  }

  private commandItems(group: 'Pages' | 'Actions', q: string): PaletteItem[] {
    return this.registry
      .commands()
      .filter((c) => c.group === group)
      .map((c) => ({ c, score: commandScore(q, c.label, c.keywords) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .map(({ c }) => ({ id: c.id, group, label: c.label, hint: c.hint, run: c.run }));
  }

  private strategyItems(q: string): PaletteItem[] {
    if (!this.strategies.hasValue()) return [];
    return this.strategies
      .value()
      .items.map((s) => ({ s, score: commandScore(q, s.id, [className(s)]) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .map(({ s }) => ({
        id: `strategy.${s.id}`,
        group: 'Strategies',
        label: s.id,
        detail: `${capitalize(s.status)} · ${className(s)}`,
        run: () => void this.router.navigate(['/strategies', s.id]),
      }));
  }

  private tickerItems(): PaletteItem[] {
    if (!this.tickers.hasValue()) return [];
    return this.tickers.value().items.map((i: InstrumentView) => ({
      id: `ticker.${i.id}`,
      group: 'Tickers',
      label: i.id,
      detail: [i.name, i.exchange].filter(Boolean).join(' · ') || undefined,
      run: () => void this.router.navigate(['/data'], { queryParams: { instrument: i.id } }),
    }));
  }

  private jobItems(q: string): PaletteItem[] {
    if (!this.jobs.hasValue()) return [];
    return this.jobs
      .value()
      .items.map((j) => {
        const label = jobLabel(j.kind);
        const strategy = jobStrategy(j);
        const title = strategy ? `${label}: ${strategy}` : label;
        return { j, title, score: commandScore(q, title, [j.id, j.kind, j.status]) };
      })
      .filter((x) => x.score > 0)
      .map(({ j, title }) => ({
        id: `job.${j.id}`,
        group: 'Recent jobs',
        label: title,
        detail: `${capitalize(j.status)} · ${formatAgo(j.created_at)}`,
        run: () => void this.router.navigateByUrl(jobPath(j.kind)),
      }));
  }
}

function className(s: StrategySummary): string {
  return s.class_path.split(/[:.]/).at(-1) ?? s.class_path;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
