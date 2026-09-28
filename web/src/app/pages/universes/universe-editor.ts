import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
  untracked,
} from '@angular/core';

import type { IndexHistoryImport, MetricView, UniverseView } from '../../api/models';
import { ScreenerService } from '../../api/screener.service';
import { UniversesService } from '../../api/universes.service';
import { ToastService } from '../../core/notify/toast.service';
import { formatNumber } from '../../core/format/format';
import { SOURCE_LABELS } from '../data/data-labels';
import { ScreenFilters } from '../screener/screen-filters';
import {
  ASSET_CLASSES,
  type AssetClass,
  type FilterRow,
  type ScreenForm,
} from '../screener/screen-form';
import {
  DEFAULT_FIELDS,
  KIND_HINT,
  KIND_LABEL,
  type KindFields,
  REBALANCE_LABEL,
  type Rebalance,
  type SpecSource,
  type UniverseForm,
  type UniverseKind,
  defaultScreen,
  formFromUniverse,
  readFileText,
  specTemplate,
  universeCreateBody,
  universeFormErrors,
  universeUpdateBody,
} from './universe-form';

function blankForm(): UniverseForm {
  return {
    id: '',
    name: '',
    description: '',
    kind: 'list',
    source: 'fields',
    fields: { ...DEFAULT_FIELDS, screen: defaultScreen() },
    specText: specTemplate('list'),
    csv: '',
  };
}

/**
 * The universe form, for a new universe or an edit. Each kind asks for its
 * own fields: tickers or a CSV for a list, an exchange picked from the ones
 * we hold, a screen (the screener's filter builder) for a rule, an index
 * with an optional history file. JSON is there for settings the fields do
 * not show, and an edit opens in JSON when the fields cannot hold the
 * stored definition, so a save never drops a setting.
 */
@Component({
  selector: 'app-universe-editor',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ScreenFilters],
  templateUrl: './universe-editor.html',
  styleUrl: './universe-editor.scss',
})
export class UniverseEditor {
  private readonly api = inject(UniversesService);
  private readonly screener = inject(ScreenerService);
  private readonly toasts = inject(ToastService);

  readonly mode = input<'create' | 'edit'>('create');
  /** The universe to edit. */
  readonly universe = input<UniverseView | null>(null);
  /** Stored universes a rule can start from. */
  readonly universes = input<readonly UniverseView[]>([]);
  readonly saved = output<UniverseView>();
  readonly cancelled = output<void>();

  protected readonly kinds: readonly UniverseKind[] = ['list', 'exchange', 'rule', 'index'];
  protected readonly kindLabel = KIND_LABEL;
  protected readonly kindHint = KIND_HINT;
  protected readonly rebalances = Object.entries(REBALANCE_LABEL) as [Rebalance, string][];
  protected readonly assetClasses = ASSET_CLASSES;
  protected readonly dataSources = Object.entries(SOURCE_LABELS).filter(
    ([id]) => id !== 'defillama',
  );

  protected readonly form = signal<UniverseForm>(blankForm());
  /** An edit waits for the stored definition (and the metrics, for a rule). */
  private readonly loaded = signal(false);
  protected readonly ready = computed(() => this.mode() === 'create' || this.loaded());

  private readonly needsMetrics = computed(
    () => this.form().kind === 'rule' || this.universe()?.kind === 'rule',
  );
  /** Screen metrics, read only for rule universes. */
  protected readonly metrics = resource({
    params: () => (this.needsMetrics() ? {} : undefined),
    loader: () => this.screener.metrics(),
  });
  protected readonly metricList = computed<MetricView[]>(() =>
    this.metrics.hasValue() ? this.metrics.value() : [],
  );
  protected readonly sortMetrics = computed(() =>
    [...this.metricList()].sort((a, b) => (a.group === b.group ? 0 : a.group === 'price' ? -1 : 1)),
  );

  /** Exchanges our instruments name, read only for exchange universes. */
  private readonly needsExchanges = computed(
    () => this.form().kind === 'exchange' && this.form().source === 'fields',
  );
  protected readonly exchanges = resource({
    params: () => (this.needsExchanges() ? {} : undefined),
    loader: () => this.api.exchanges(),
  });
  protected readonly exchangeLabel = (e: { exchange: string; instruments: number }) =>
    `${e.exchange}: ${formatNumber(e.instruments, { digits: 0 })} instruments`;

  /** Universes a rule may start from: every other stored one. */
  protected readonly startFrom = computed(() =>
    this.universes().filter((u) => u.id !== this.form().id),
  );

  protected readonly submitted = signal(false);
  protected readonly saving = signal(false);
  protected readonly csvName = signal<string | null>(null);
  protected readonly errors = computed(() => universeFormErrors(this.form(), this.metricList()));
  protected readonly hasErrors = computed(() => Object.keys(this.errors()).length > 0);

  /** index: an optional history file imported before the save. */
  protected readonly indexFile = signal<string | null>(null);
  private readonly indexContent = signal('');
  private readonly indexFormat = signal<IndexHistoryImport['format']>('csv');

  constructor() {
    effect(() => {
      const u = this.universe();
      if (this.mode() !== 'edit' || !u || untracked(this.loaded)) return;
      if (u.kind === 'rule' && !this.metrics.hasValue() && !this.metrics.error()) return;
      const metrics = this.metricList();
      untracked(() => {
        this.form.set(formFromUniverse(u, metrics));
        this.loaded.set(true);
      });
    });
  }

  protected patch(change: Partial<UniverseForm>): void {
    this.form.update((f) => ({ ...f, ...change }));
  }

  protected patchFields(change: Partial<KindFields>): void {
    this.form.update((f) => ({ ...f, fields: { ...f.fields, ...change } }));
  }

  protected patchScreen(change: Partial<ScreenForm>): void {
    this.form.update((f) => ({
      ...f,
      fields: { ...f.fields, screen: { ...f.fields.screen, ...change } },
    }));
  }

  protected setFilters(filters: FilterRow[]): void {
    this.patchScreen({ filters });
  }

  protected toggleAssetClass(id: AssetClass, on: boolean): void {
    const now = this.form().fields.screen.assetClasses.filter((c) => c !== id);
    this.patchScreen({ assetClasses: on ? [...now, id] : now });
  }

  protected setKind(kind: UniverseKind): void {
    this.form.update((f) => ({
      ...f,
      kind,
      // CSV is for list universes only. JSON restarts from the new kind's fields.
      source: f.source === 'csv' && kind !== 'list' ? 'fields' : f.source,
      specText: specTemplate(kind, f.fields, this.metricList()),
    }));
  }

  /** Advanced: edit the definition as JSON, starting from the fields. */
  protected editAsJson(): void {
    this.form.update((f) => ({
      ...f,
      source: 'json',
      specText: specTemplate(f.kind, f.fields, this.metricList()),
    }));
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

  protected async pickIndexFile(input: HTMLInputElement): Promise<void> {
    const file = input.files?.[0];
    if (!file) return;
    this.indexFile.set(file.name);
    this.indexFormat.set(file.name.toLowerCase().endsWith('.json') ? 'json' : 'csv');
    this.indexContent.set(await readFileText(file));
  }

  async save(): Promise<void> {
    this.submitted.set(true);
    if (this.hasErrors() || this.saving() || !this.ready()) return;
    const f = this.form();
    const metrics = this.metricList();
    this.saving.set(true);
    try {
      await this.importIndexFirst(f);
      const view =
        this.mode() === 'edit'
          ? await this.api.update(f.id, universeUpdateBody(f, metrics))
          : await this.api.create(universeCreateBody(f, metrics));
      this.submitted.set(false);
      this.saved.emit(view);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
    }
  }

  /** An index universe with a chosen history file stores the history first. */
  private async importIndexFirst(f: UniverseForm): Promise<void> {
    const content = this.indexContent();
    if (f.kind !== 'index' || f.source !== 'fields' || !content.trim()) return;
    const view = await this.api.importIndexHistory({
      index_id: f.fields.indexId.trim(),
      format: this.indexFormat(),
      content,
    });
    this.toasts.success(
      `Imported ${view.constituents} members and ${view.changes} changes for ${view.index_id}.`,
    );
    this.indexContent.set('');
    this.indexFile.set(null);
  }
}
