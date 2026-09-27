import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import type { EnsureDataRequest, EnsureReport, UniverseRefreshView } from '../../api/models';
import { UniversesService } from '../../api/universes.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { formatDate, isoDay } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { DateTimePipe, NumPipe } from '../../shared/format.pipes';
import { DataTable, type TableColumn } from '../../shared/ui/data-table/data-table';
import { JobProgress, JobResult } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { sourceLabel } from '../data/data-labels';
import { KIND_LABEL } from './universe-form';

type EnsureSource = '' | NonNullable<EnsureDataRequest['source']>;
const SOURCES: readonly { value: EnsureSource; label: string }[] = [
  { value: '', label: 'Default source' },
  ...(['eodhd', 'yahoo', 'defillama'] as const).map((value) => ({
    value,
    label: sourceLabel(value),
  })),
];

const INTERVALS = ['1d', '1w', '1h', '4h', '30m', '15m', '5m', '1m'] as const;
/** Members per page: thousands of tickers never render at once. */
export const MEMBERS_PAGE_SIZE = 50;

interface MemberRow {
  ticker: string;
}

/**
 * One universe: members on a date (a paged table with a finder), refresh
 * (definition to membership rows) and Fetch missing data (only the missing
 * bars), both background jobs followed to completion with their result
 * reads retryable, and delete.
 */
@Component({
  selector: 'app-universe-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    JobProgress,
    LoadingState,
    EmptyState,
    ErrorState,
    RouterLink,
    DateTimePipe,
    NumPipe,
    PermissionNote,
    DataTable,
  ],
  templateUrl: './universe-detail.page.html',
  styleUrl: './universe-detail.page.scss',
})
export class UniverseDetailPage {
  private readonly api = inject(UniversesService);
  private readonly jobs = inject(JobsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly session = inject(SessionService);

  /** Refresh and Ensure data are lab work; delete breaks strategies, so admins only. */
  protected readonly canRefresh = computed(() => this.session.can('lab.run'));
  protected readonly canDelete = computed(() => this.session.can('strategy.promote'));

  readonly id = input.required<string>();

  protected readonly universe = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.api.get(params.id),
  });

  protected readonly asOf = signal(isoDay());
  protected readonly members = resource({
    params: () => ({ id: this.id(), asOf: this.asOf() }),
    loader: ({ params }) => this.api.members(params.id, params.asOf),
  });

  /** Narrows the member table to tickers containing this text. */
  protected readonly find = signal('');
  protected readonly memberRows = computed<MemberRow[]>(() => {
    if (!this.members.hasValue()) return [];
    const needle = this.find().trim().toUpperCase();
    const tickers = this.members.value().tickers;
    return (needle ? tickers.filter((t) => t.toUpperCase().includes(needle)) : tickers).map(
      (ticker) => ({ ticker }),
    );
  });
  protected readonly memberColumns: TableColumn<MemberRow>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
  ];
  protected readonly memberKey = (m: MemberRow) => m.ticker;
  protected readonly membersPageSize = MEMBERS_PAGE_SIZE;
  protected readonly day = formatDate;

  protected readonly kindLabel = KIND_LABEL;
  protected readonly sources = SOURCES;
  protected readonly intervals = INTERVALS;
  /** The universe's name, else its id. */
  protected readonly name = computed(
    () => (this.universe.hasValue() && this.universe.value().name) || this.id(),
  );
  protected readonly specJson = computed(() =>
    this.universe.hasValue() ? JSON.stringify(this.universe.value().spec, null, 2) : '',
  );

  // ---- refresh ------------------------------------------------------------
  protected readonly refreshRun = signal<JobHandle | null>(null);
  /** The refresh's counts; a failed read offers Try again. */
  protected readonly refreshResult = new JobResult<UniverseRefreshView>();
  protected readonly refreshing = signal(false);

  async refresh(): Promise<void> {
    if (this.refreshing() || !this.canRefresh()) return;
    const id = this.id();
    const ok = await this.confirm.confirm({
      title: `Refresh ${this.name()}?`,
      message:
        'Rebuilds the membership from the definition. Running it twice changes nothing. Lab runs and trading runs use the new members.',
      confirmLabel: 'Refresh',
    });
    if (!ok) return;
    this.refreshing.set(true);
    this.refreshResult.reset();
    try {
      const job = await this.api.refresh(id);
      this.refreshRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.refreshRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        this.universe.reload();
        this.members.reload();
        const result = await this.refreshResult.load(() => this.api.refreshResult(job.id));
        if (result) {
          this.toasts.success(`Refreshed ${this.name()}: ${result.current_members} members today.`);
        }
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.refreshing.set(false);
    }
  }

  // ---- ensure data --------------------------------------------------------
  protected readonly start = signal(isoDay(new Date(Date.now() - 365 * 86_400_000)));
  protected readonly end = signal(isoDay());
  protected readonly interval = signal('1d');
  protected readonly source = signal<'' | NonNullable<EnsureDataRequest['source']>>('');
  protected readonly ensureSubmitted = signal(false);
  protected readonly windowError = computed(() =>
    this.start() && this.end() && this.start() <= this.end()
      ? null
      : 'The start date must be on or before the end date.',
  );
  protected readonly ensureRun = signal<JobHandle | null>(null);
  /** What the fetch did; a failed read offers Try again. */
  protected readonly ensureResult = new JobResult<EnsureReport>();
  protected readonly ensuring = signal(false);

  async ensure(): Promise<void> {
    this.ensureSubmitted.set(true);
    if (!this.canRefresh() || this.windowError() || this.ensuring()) return;
    const id = this.id();
    const body: EnsureDataRequest = {
      start: this.start(),
      end: this.end(),
      interval: this.interval(),
      source: this.source() || null,
    };
    const ok = await this.confirm.confirm({
      title: `Fetch missing data for ${this.name()}?`,
      message: `Fetches only the ${body.interval} prices our data is missing for the members from ${formatDate(body.start)} to ${formatDate(body.end)}. Paid data plans may charge per call.`,
      confirmLabel: 'Fetch missing data',
    });
    if (!ok) return;
    this.ensuring.set(true);
    this.ensureResult.reset();
    try {
      const job = await this.api.ensure(id, body);
      this.ensureRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.ensureRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const report = await this.ensureResult.load(() => this.api.ensureResult(job.id));
        if (report) {
          this.toasts.success(
            `Fetched ${report.tickers_fetched} tickers, ${report.tickers_up_to_date} already up to date.`,
          );
        }
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.ensuring.set(false);
    }
  }

  // ---- delete -------------------------------------------------------------
  protected readonly deleting = signal(false);

  async remove(): Promise<void> {
    if (!this.canDelete()) return;
    const id = this.id();
    const ok = await this.confirm.confirm({
      title: `Delete ${this.name()}?`,
      message:
        'Removes the definition and its membership rows. Strategies and lab runs that name it stop working.',
      confirmLabel: 'Delete universe',
      tone: 'danger',
      typedConfirmation: id,
    });
    if (!ok) return;
    this.deleting.set(true);
    try {
      await this.api.delete(id);
      this.toasts.success(`Deleted ${this.name()}.`);
      await this.router.navigate(['/universes']);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.deleting.set(false);
    }
  }
}
