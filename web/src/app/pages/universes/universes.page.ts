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
import { SOURCE_LABELS } from '../data/data-labels';
import {
  ASSET_CLASS_LABEL,
  DEFAULT_FIELDS,
  KIND_HINT,
  KIND_LABEL,
  type KindFields,
  REBALANCE_LABEL,
  type Rebalance,
  type SpecSource,
  type UniverseForm,
  type UniverseKind,
  readFileText,
  specTemplate,
  universeCreateBody,
  universeFormErrors,
} from './universe-form';

/**
 * Stored universes: the list with kind, member count and last refresh, a
 * create form (each kind's own fields, a CSV for lists, or the definition
 * as JSON for advanced use) and the index history import.
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

  protected readonly kinds: readonly UniverseKind[] = ['list', 'exchange', 'rule', 'index'];
  protected readonly kindLabel = KIND_LABEL;
  protected readonly kindHint = KIND_HINT;
  protected readonly rebalances = Object.entries(REBALANCE_LABEL) as [Rebalance, string][];
  protected readonly assetClasses = Object.entries(ASSET_CLASS_LABEL);
  protected readonly dataSources = Object.entries(SOURCE_LABELS).filter(
    ([id]) => id !== 'defillama',
  );
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
  protected readonly saving = signal(false);
  protected readonly submitted = signal(false);
  protected readonly form = signal<UniverseForm>({
    id: '',
    name: '',
    description: '',
    kind: 'list',
    source: 'fields',
    fields: { ...DEFAULT_FIELDS },
    specText: specTemplate('list'),
    csv: '',
  });
  protected readonly csvName = signal<string | null>(null);
  protected readonly errors = computed(() => universeFormErrors(this.form()));
  protected readonly hasErrors = computed(() => Object.keys(this.errors()).length > 0);

  protected patch(change: Partial<UniverseForm>): void {
    this.form.update((f) => ({ ...f, ...change }));
  }

  protected patchFields(change: Partial<KindFields>): void {
    this.form.update((f) => ({ ...f, fields: { ...f.fields, ...change } }));
  }

  protected toggleAssetClass(id: string, on: boolean): void {
    const now = this.form().fields.assetClasses.filter((c) => c !== id);
    this.patchFields({ assetClasses: on ? [...now, id] : now });
  }

  protected setKind(kind: UniverseKind): void {
    this.form.update((f) => ({
      ...f,
      kind,
      // CSV is for list universes only; JSON restarts from the new kind's fields.
      source: f.source === 'csv' && kind !== 'list' ? 'fields' : f.source,
      specText: specTemplate(kind, f.fields),
    }));
  }

  /** Advanced: edit the definition as JSON, starting from the fields. */
  protected editAsJson(): void {
    this.form.update((f) => ({ ...f, source: 'json', specText: specTemplate(f.kind, f.fields) }));
  }

  protected setSource(source: SpecSource): void {
    this.patch({ source });
  }

  protected async pickCsv(input: HTMLInputElement): Promise<void> {
    const file = input.files?.[0];
    if (!file) return;
    this.csvName.set(file.name);
    this.patch({ csv: await readFileText(file) });
  }

  async create(): Promise<void> {
    this.submitted.set(true);
    if (!this.canEdit() || this.hasErrors() || this.saving()) return;
    const body = universeCreateBody(this.form());
    this.saving.set(true);
    try {
      const created = await this.api.create(body);
      this.toasts.success(`Created ${created.id}. Refresh it to fill its members.`);
      this.creating.set(false);
      this.submitted.set(false);
      this.universes.reload();
      await this.router.navigate(['/universes', created.id]);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
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
