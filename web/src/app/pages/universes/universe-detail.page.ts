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
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { DateTimePipe, NumPipe } from '../../shared/format.pipes';
import { JobProgress } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { KIND_LABEL } from './universe-form';

const SOURCES: readonly { value: '' | NonNullable<EnsureDataRequest['source']>; label: string }[] =
  [
    { value: '', label: 'Default source' },
    { value: 'eodhd', label: 'EODHD' },
    { value: 'yahoo', label: 'Yahoo Finance' },
    { value: 'defillama', label: 'DefiLlama' },
  ];

const INTERVALS = ['1d', '1w', '1h', '4h', '30m', '15m', '5m', '1m'] as const;

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/**
 * One universe: members on a date, refresh (definition to membership rows)
 * and ensure data (fetch only the missing bars), both background jobs
 * followed to completion, and delete.
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

  readonly id = input.required<string>();

  protected readonly universe = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => this.api.get(params.id),
  });

  protected readonly asOf = signal(isoDay(new Date()));
  protected readonly members = resource({
    params: () => ({ id: this.id(), asOf: this.asOf() }),
    loader: ({ params }) => this.api.members(params.id, params.asOf),
  });

  protected readonly kindLabel = KIND_LABEL;
  protected readonly sources = SOURCES;
  protected readonly intervals = INTERVALS;
  protected readonly specJson = computed(() =>
    this.universe.hasValue() ? JSON.stringify(this.universe.value().spec, null, 2) : '',
  );

  // ---- refresh ------------------------------------------------------------
  protected readonly refreshRun = signal<JobHandle | null>(null);
  protected readonly refreshResult = signal<UniverseRefreshView | null>(null);
  protected readonly refreshing = signal(false);

  async refresh(): Promise<void> {
    if (this.refreshing()) return;
    const id = this.id();
    const ok = await this.confirm.confirm({
      title: `Refresh ${id}?`,
      message:
        'Rebuilds the membership from the definition. Running it twice changes nothing. Lab runs and ticks use the new members.',
      confirmLabel: 'Refresh',
    });
    if (!ok) return;
    this.refreshing.set(true);
    this.refreshResult.set(null);
    try {
      const job = await this.api.refresh(id);
      this.refreshRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.refreshRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const result = await this.api.refreshResult(job.id);
        this.refreshResult.set(result);
        this.toasts.success(`Refreshed ${id}: ${result.current_members} members today.`);
        this.universe.reload();
        this.members.reload();
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.refreshing.set(false);
    }
  }

  // ---- ensure data --------------------------------------------------------
  protected readonly start = signal(isoDay(new Date(Date.now() - 365 * 86_400_000)));
  protected readonly end = signal(isoDay(new Date()));
  protected readonly interval = signal('1d');
  protected readonly source = signal<'' | NonNullable<EnsureDataRequest['source']>>('');
  protected readonly ensureSubmitted = signal(false);
  protected readonly windowError = computed(() =>
    this.start() && this.end() && this.start() <= this.end()
      ? null
      : 'The start date must be on or before the end date.',
  );
  protected readonly ensureRun = signal<JobHandle | null>(null);
  protected readonly ensureResult = signal<EnsureReport | null>(null);
  protected readonly ensuring = signal(false);

  async ensure(): Promise<void> {
    this.ensureSubmitted.set(true);
    if (this.windowError() || this.ensuring()) return;
    const id = this.id();
    const body: EnsureDataRequest = {
      start: this.start(),
      end: this.end(),
      interval: this.interval(),
      source: this.source() || null,
    };
    const ok = await this.confirm.confirm({
      title: `Fetch missing data for ${id}?`,
      message: `Fetches only the ${body.interval} bars the lake lacks for the members from ${body.start} to ${body.end}. Paid data plans may charge per call.`,
      confirmLabel: 'Ensure data',
    });
    if (!ok) return;
    this.ensuring.set(true);
    this.ensureResult.set(null);
    try {
      const job = await this.api.ensure(id, body);
      this.ensureRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.ensureRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const report = await this.api.ensureResult(job.id);
        this.ensureResult.set(report);
        this.toasts.success(
          `Fetched ${report.tickers_fetched} tickers, ${report.tickers_up_to_date} already up to date.`,
        );
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
    const id = this.id();
    const ok = await this.confirm.confirm({
      title: `Delete ${id}?`,
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
      this.toasts.success(`Deleted ${id}.`);
      await this.router.navigate(['/universes']);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.deleting.set(false);
    }
  }
}
