import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  MetricView,
  SavedScreenView,
  ScreenResult,
  ScreenRow,
  ScreenRunRequest,
  ScreenSize,
  ScreenSpec,
  ScreenUniverseView,
} from '../../api/models';
import { JobsApiService } from '../../api/jobs-api.service';
import { ScreenerService } from '../../api/screener.service';
import { UniversesService } from '../../api/universes.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { formatDate, formatNumber } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { JobProgress, JobResult } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { SaveUniverseSheet } from './save-universe-sheet';
import { ScreenAlerts } from './screen-alerts';
import { ScreenFilters } from './screen-filters';
import {
  ASSET_CLASSES,
  type AssetClass,
  type ScreenForm,
  emptyForm,
  formatMetric,
  fromSpec,
  toSpec,
} from './screen-form';

const ORDER: SegmentOption<'desc' | 'asc'>[] = [
  { value: 'desc', label: 'Highest first' },
  { value: 'asc', label: 'Lowest first' },
];

/** The screen that is open: saved (with its id) or new. */
interface OpenScreen {
  id: string;
  name: string;
  /** The spec as saved, to tell whether the form changed since. */
  saved: string;
}

/**
 * The screener: filter instruments on price and fundamental metrics, on
 * the data as it was on a date, then keep the screen, or store it as a
 * universe for the lab.
 */
@Component({
  selector: 'app-screener-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PermissionNote,
    Segmented,
    DataTable,
    TableCell,
    LoadingState,
    ErrorState,
    EmptyState,
    SaveUniverseSheet,
    ScreenAlerts,
    JobProgress,
    ScreenFilters,
  ],
  templateUrl: './screener.page.html',
  styleUrl: './screener.page.scss',
})
export class ScreenerPage {
  private readonly api = inject(ScreenerService);
  private readonly universesApi = inject(UniversesService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly jobs = inject(JobsService);
  private readonly jobsApi = inject(JobsApiService);
  private readonly destroyRef = inject(DestroyRef);

  private readonly sheet = viewChild.required(SaveUniverseSheet);

  protected readonly assetClasses = ASSET_CLASSES;
  protected readonly order = ORDER;

  protected readonly metrics = resource({ loader: () => this.api.metrics() });
  protected readonly screens = resource({ loader: () => this.api.list() });
  protected readonly universes = resource({ loader: () => this.universesApi.list() });

  protected readonly form = signal<ScreenForm>(emptyForm());
  protected readonly asOf = signal('');
  protected readonly screenName = signal('');
  protected readonly open = signal<OpenScreen | null>(null);
  protected readonly tried = signal(false);
  protected readonly running = signal(false);
  protected readonly saving = signal(false);
  protected readonly result = signal<ScreenResult | null>(null);
  protected readonly savedUniverse = signal<ScreenUniverseView | null>(null);
  /** The size check of the last run: over the cap, or big enough for a job. */
  protected readonly size = signal<ScreenSize | null>(null);
  /** The background job of a large screen, while it runs and after. */
  protected readonly job = signal<JobHandle | null>(null);
  protected readonly jobRead = new JobResult<ScreenResult>();
  protected readonly cancelling = signal(false);

  protected readonly canSave = computed(() => this.session.can('portfolio.manage'));
  protected readonly canUniverse = computed(() => this.session.can('lab.run'));

  protected readonly metricList = computed<MetricView[]>(() =>
    this.metrics.hasValue() ? this.metrics.value() : [],
  );
  protected readonly priceMetrics = computed(() =>
    this.metricList().filter((m) => m.group === 'price'),
  );
  protected readonly fundamentalMetrics = computed(() =>
    this.metricList().filter((m) => m.group === 'fundamental'),
  );

  protected readonly checked = computed(() => toSpec(this.form(), this.metricList()));
  protected readonly errors = computed(() => (this.tried() ? this.checked().errors : []));
  /** The open screen is unchanged since it was saved or opened. */
  protected readonly unchanged = computed(() => {
    const o = this.open();
    return !!o && o.saved === JSON.stringify(this.checked().spec) && o.name === this.screenName();
  });

  protected readonly columns = computed<TableColumn<ScreenRow>[]>(() => {
    const r = this.result();
    const cols: TableColumn<ScreenRow>[] = [
      { key: 'ticker', label: 'Ticker', mobile: 'title' },
      { key: 'name', label: 'Name' },
      { key: 'sector', label: 'Sector', mobile: 'hide' },
      { key: 'exchange', label: 'Exchange', mobile: 'hide' },
    ];
    for (const id of r?.metrics ?? []) {
      const m = this.metricList().find((x) => x.id === id);
      const unit = m?.unit ?? 'ratio';
      cols.push({
        key: `m:${id}`,
        label: m?.label ?? id,
        format: 'number',
        help: false,
        value: (row) => row.values[id] ?? null,
        display: (row) => formatMetric(row.values[id], unit),
      });
    }
    return cols;
  });

  protected readonly rowKey = (r: ScreenRow) => r.ticker;
  protected readonly day = (v: string) => formatDate(v);

  /** Why a screen over the candidate cap did not run, and how to narrow it. */
  protected readonly capMessage = computed(() => {
    const z = this.size();
    if (!z?.over_cap) return '';
    return (
      `This screen starts from ${formatNumber(z.candidates)} candidates, more than the ` +
      `limit of ${formatNumber(z.max_candidates)}. Narrow it: start from a universe, or ` +
      'pick asset classes, sectors, exchanges, a minimum price or a minimum dollar volume.'
    );
  });

  protected readonly jobNote = computed(() => {
    const z = this.size();
    return z?.use_job
      ? `A large screen: ${formatNumber(z.candidates)} candidates. It runs in the background.`
      : '';
  });

  protected readonly resultSummary = computed(() => {
    const r = this.result();
    if (!r) return '';
    const matched = r.matched === 1 ? '1 match' : `${formatNumber(r.matched)} matches`;
    return `${matched} out of ${formatNumber(r.candidates)}, on ${formatDate(r.as_of)}.`;
  });

  // ---- the form ---------------------------------------------------------------------

  protected patch(change: Partial<ScreenForm>): void {
    this.form.update((f) => ({ ...f, ...change }));
  }

  protected toggleClass(value: AssetClass, on: boolean): void {
    const now = this.form().assetClasses.filter((c) => c !== value);
    this.patch({ assetClasses: on ? [...now, value] : now });
  }

  protected toggleColumn(id: string, on: boolean): void {
    const now = this.form().columns.filter((c) => c !== id);
    this.patch({ columns: on ? [...now, id] : now });
  }

  protected startOver(): void {
    this.form.set(emptyForm());
    this.open.set(null);
    this.screenName.set('');
    this.tried.set(false);
    this.result.set(null);
    this.savedUniverse.set(null);
    this.size.set(null);
  }

  // ---- run -------------------------------------------------------------------------

  /**
   * Check the size first. Over the cap nothing runs and the page says how
   * to narrow the screen. Above the job threshold the screen runs as a
   * background job with progress. Otherwise it runs directly.
   */
  async run(): Promise<void> {
    this.tried.set(true);
    const { spec, errors } = this.checked();
    if (errors.length) return;
    const body: ScreenRunRequest = { spec, as_of: this.asOf() || null };
    this.running.set(true);
    this.job()?.stop();
    this.job.set(null);
    this.jobRead.reset();
    try {
      const size = await this.api.size(body);
      this.size.set(size);
      if (size.over_cap) {
        this.result.set(null);
      } else if (size.use_job) {
        await this.runAsJob(body);
      } else {
        this.result.set(await this.api.run(body));
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.running.set(false);
    }
  }

  private async runAsJob(body: ScreenRunRequest): Promise<void> {
    this.result.set(null);
    const queued = await this.api.submitJob(body);
    const handle = this.jobs.track(queued.id, this.destroyRef);
    this.job.set(handle);
    const last = await handle.finished;
    if (last?.status !== 'succeeded') return;
    const result = await this.jobRead.load(() => this.api.jobResult(queued.id));
    if (result) this.result.set(result);
  }

  protected async cancelJob(handle: JobHandle): Promise<void> {
    this.cancelling.set(true);
    try {
      await this.jobsApi.cancel(handle.jobId);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.cancelling.set(false);
    }
  }

  // ---- saved screens ------------------------------------------------------------------

  async openScreen(s: SavedScreenView): Promise<void> {
    try {
      const fresh = await this.api.get(s.id);
      this.load(fresh);
      this.result.set(null);
      this.savedUniverse.set(null);
      this.toasts.info(`Opened ${fresh.name}. Run it to see today's matches.`);
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  private load(s: SavedScreenView): void {
    const form = fromSpec(s.spec, this.metricList());
    this.form.set(form);
    this.screenName.set(s.name);
    this.tried.set(false);
    // Compare against the spec as this form would send it.
    const spec = toSpec(form, this.metricList()).spec;
    this.open.set({ id: s.id, name: s.name, saved: JSON.stringify(spec) });
  }

  /** Save changes to the open screen, or save a new one. */
  async save(asNew = false): Promise<void> {
    this.tried.set(true);
    const { spec, errors } = this.checked();
    const name = this.screenName().trim();
    if (errors.length) return;
    if (!name) {
      this.toasts.info('Name the screen first.');
      return;
    }
    const open = asNew ? null : this.open();
    this.saving.set(true);
    try {
      const saved = open
        ? await this.api.update(open.id, { name, spec })
        : await this.api.create({ name, spec });
      this.load(saved);
      this.screens.reload();
      this.toasts.success(open ? `Saved the changes to ${saved.name}.` : `Saved ${saved.name}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
    }
  }

  async remove(s: SavedScreenView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Delete ${s.name}?`,
      message: 'The screen goes. Universes made from it stay.',
      confirmLabel: 'Delete screen',
      tone: 'danger',
    });
    if (!ok) return;
    try {
      await this.api.delete(s.id);
      if (this.open()?.id === s.id) this.open.set(null);
      this.screens.reload();
      this.toasts.success(`Deleted ${s.name}.`);
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  // ---- universe ----------------------------------------------------------------------

  async saveAsUniverse(): Promise<void> {
    this.tried.set(true);
    const { spec, errors } = this.checked();
    if (errors.length) return;
    const open = this.open();
    const screenId = open && this.unchanged() ? open.id : null;
    const saved = await this.sheet().open(
      spec as ScreenSpec,
      screenId,
      this.screenName() || 'My screen',
    );
    if (!saved) return;
    this.savedUniverse.set(saved);
    this.toasts.success(
      `Saved the universe ${saved.universe.name ?? saved.universe.id}. Its members appear when the refresh ends.`,
    );
  }
}
