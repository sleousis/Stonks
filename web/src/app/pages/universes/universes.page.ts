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
import { DateTimePipe } from '../../shared/format.pipes';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { UniverseEditor } from './universe-editor';
import { KIND_LABEL, isStale, readFileText } from './universe-form';

/**
 * Stored universes: the list with kind, member count and last refresh
 * (marked when the definition changed since), the create form
 * (`<app-universe-editor>`) and the index history import.
 */
@Component({
  selector: 'app-universes-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    DataTable,
    TableCell,
    LoadingState,
    EmptyState,
    ErrorState,
    RouterLink,
    DateTimePipe,
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

  protected readonly universes = resource({ loader: () => this.api.list() });

  protected readonly universeKey = (u: UniverseView) => u.id;

  protected readonly columns: TableColumn<UniverseView>[] = [
    { key: 'name', label: 'Universe', value: (u) => u.name || u.id, mobile: 'title' },
    { key: 'id', label: 'Id', mobile: 'hide' },
    { key: 'kind', label: 'Kind', value: (u) => KIND_LABEL[u.kind] },
    { key: 'member_count', label: 'Members', format: 'number' },
    { key: 'refreshed_at', label: 'Last refresh', format: 'datetime' },
  ];

  // ---- create -------------------------------------------------------------
  protected readonly creating = signal(false);

  /** The definition changed after the last refresh: the members are behind. */
  protected readonly stale = isStale;

  async created(u: UniverseView): Promise<void> {
    this.toasts.success(`Created ${u.id}. Refresh it to fill its members.`);
    this.creating.set(false);
    this.universes.reload();
    await this.router.navigate(['/universes', u.id]);
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
