import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import type { IndexHistoryImport, UniverseView } from '../../api/models';
import { UniversesService } from '../../api/universes.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { formatDateTime, formatNumber } from '../../core/format/format';
import { DateTimePipe, NumPipe } from '../../shared/format.pipes';
import { StatTile } from '../../shared/ui/stat-tile';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { UniverseEditor } from './universe-editor';
import { KIND_LABEL, isStale, readFileText } from './universe-form';

type KindFilter = 'all' | UniverseView['kind'];

/**
 * Stored universes: the list with kind, member count and last refresh
 * (marked when the definition changed since), the create form
 * (`<app-universe-editor>`) and the index history import.
 */
@Component({
  selector: 'app-universes-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    StatTile,
    PageHeader,
    DataTable,
    TableCell,
    LoadingState,
    EmptyState,
    ErrorState,
    RouterLink,
    DateTimePipe,
    NumPipe,
    PermissionNote,
    UniverseEditor,
  ],
  templateUrl: './universes.page.html',
  styleUrl: './universes.page.scss',
})
export class UniversesPage {
  private readonly api = inject(UniversesService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);
  private readonly session = inject(SessionService);

  /** Creating universes and importing index history (lab work). */
  protected readonly canEdit = computed(() => this.session.can('lab.run'));
  /** Admins see the Advanced parts: index history and the definition as JSON. */
  protected readonly isAdmin = computed(() => this.session.can('operations.run'));

  protected readonly universes = resource({ loader: () => this.api.list() });

  protected readonly universeKey = (u: UniverseView) => u.id;

  protected readonly columns: TableColumn<UniverseView>[] = [
    { key: 'name', label: 'Universe', value: (u) => u.name || u.id, mobile: 'title' },
    { key: 'id', label: 'Id', mobile: 'hide', help: false },
    { key: 'kind', label: 'Kind', value: (u) => KIND_LABEL[u.kind], mobile: 'inline' },
    { key: 'member_count', label: 'Members', format: 'number', mobile: 'figure' },
    { key: 'refreshed_at', label: 'Last refresh', format: 'datetime' },
  ];

  protected readonly kindLabel = KIND_LABEL;
  protected readonly kindFilter = signal<KindFilter>('all');
  protected readonly kinds = computed(() => {
    const rows = this.universes.hasValue() ? this.universes.value() : [];
    const count = (k: KindFilter) =>
      k === 'all' ? rows.length : rows.filter((u) => u.kind === k).length;
    return (['all', 'list', 'exchange', 'rule', 'index'] as const).map((k) => ({
      value: k,
      label: k === 'all' ? 'All' : KIND_LABEL[k],
      count: count(k),
    }));
  });
  protected readonly shown = computed(() => {
    const rows = this.universes.hasValue() ? this.universes.value() : [];
    const k = this.kindFilter();
    return k === 'all' ? rows : rows.filter((u) => u.kind === k);
  });

  /** The summary strip: how many, how many members, how many behind, the last refresh. */
  protected readonly summary = computed(() => {
    const rows = this.universes.hasValue() ? this.universes.value() : [];
    const members = rows.reduce((n, u) => n + (u.member_count ?? 0), 0);
    const behind = rows.filter((u) => isStale(u) || !u.refreshed_at).length;
    const last = rows
      .map((u) => u.refreshed_at)
      .filter((d): d is string => !!d)
      .sort()
      .at(-1);
    return { count: rows.length, members, behind, last: last ? formatDateTime(last) : 'Never' };
  });

  /** "5 universes with 1,240 members. 1 needs a refresh." */
  protected readonly standfirst = computed(() => {
    if (!this.universes.hasValue()) {
      return 'Ticker lists a strategy may trade or be tested on, with every past member kept.';
    }
    const s = this.summary();
    if (!s.count)
      return 'Save the tickers a strategy may trade or be tested on. Past members stay in.';
    const head = `${s.count} ${s.count === 1 ? 'universe' : 'universes'} with ${formatNumber(s.members)} members.`;
    if (!s.behind) return `${head} All are up to date.`;
    return `${head} ${s.behind} ${s.behind === 1 ? 'needs' : 'need'} a refresh.`;
  });

  // ---- create -------------------------------------------------------------
  protected readonly creating = signal(false);

  /** The definition changed after the last refresh: the members are behind. */
  protected readonly stale = isStale;

  /** A list fills its members at once; other kinds wait for a Refresh. */
  async created(u: UniverseView): Promise<void> {
    const name = u.name || u.id;
    this.creating.set(false);
    this.universes.reload();
    if (u.kind === 'list') {
      this.toasts.success(`Created ${name}. Filling its members now.`);
      await this.router.navigate(['/universes', u.id], { queryParams: { fill: 1 } });
    } else {
      this.toasts.success(`Created ${name}. Refresh it to fill its members.`);
      await this.router.navigate(['/universes', u.id]);
    }
  }

  // ---- index history ------------------------------------------------------
  protected readonly indexId = signal('');
  protected readonly indexFormat = signal<IndexHistoryImport['format']>('csv');
  protected readonly indexContent = signal('');
  protected readonly indexFile = signal<string | null>(null);
  protected readonly indexSubmitted = signal(false);
  protected readonly importing = signal(false);
  protected readonly indexErrors = computed(() => ({
    id: this.indexId().trim() ? null : 'Enter the index id, like sp500.',
    content: this.indexContent().trim() ? null : 'Choose a file or paste the history.',
  }));

  protected async pickIndexFile(input: HTMLInputElement): Promise<void> {
    const file = input.files?.[0];
    if (!file) return;
    this.indexFile.set(file.name);
    if (file.name.toLowerCase().endsWith('.json')) this.indexFormat.set('json');
    this.indexContent.set(await readFileText(file));
  }

  async importHistory(): Promise<void> {
    this.indexSubmitted.set(true);
    const errs = this.indexErrors();
    if (!this.canEdit() || errs.id || errs.content || this.importing()) return;
    const indexId = this.indexId().trim();
    const ok = await this.confirm.confirm({
      title: `Import history for ${indexId}?`,
      message:
        'Stores the member snapshot and dated changes for this index. Refresh its universes after.',
      confirmLabel: 'Import history',
    });
    if (!ok) return;
    this.importing.set(true);
    try {
      const view = await this.api.importIndexHistory({
        index_id: indexId,
        format: this.indexFormat(),
        content: this.indexContent(),
      });
      this.toasts.success(
        `Imported ${view.constituents} members and ${view.changes} changes for ${view.index_id}.`,
      );
      this.indexContent.set('');
      this.indexFile.set(null);
      this.indexSubmitted.set(false);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.importing.set(false);
    }
  }
}
