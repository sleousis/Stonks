import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { Job, ScheduledJobView, ScheduledRunView } from '../../api/models';
import { OperationsService } from '../../api/operations.service';
import { ScheduleService } from '../../api/schedule.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { JobProgress } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

const RECENT_RUNS = 30;

/** A scheduled job with the status of its latest run. */
export interface JobRow extends ScheduledJobView {
  last_status: string | null;
  last_run_at: string | null;
}

/** A backup job as a history row. */
export interface BackupRow {
  id: string;
  status: Job['status'];
  created_at: string;
  finished_at: string | null;
  backup_id: string | null;
  pruned: number | null;
  error: string | null;
}

export function jobRows(jobs: readonly ScheduledJobView[], recent: readonly ScheduledRunView[]) {
  return jobs.map<JobRow>((j) => {
    // `recent` comes newest first.
    const last = recent.find((r) => r.job_name === j.name);
    return { ...j, last_status: last?.status ?? null, last_run_at: last?.started_at ?? null };
  });
}

export function backupRow(job: Job): BackupRow {
  const result = (job.result ?? null) as { backup_id?: unknown; pruned?: unknown } | null;
  return {
    id: job.id,
    status: job.status,
    created_at: job.created_at,
    finished_at: job.finished_at ?? null,
    backup_id: typeof result?.backup_id === 'string' ? result.backup_id : null,
    pruned: Array.isArray(result?.pruned) ? result.pruned.length : null,
    error: job.error ?? null,
  };
}

/**
 * The built-in scheduler (jobs, next fire, recent runs, run now) and
 * server-side backups (history and Back up now, followed to completion).
 */
@Component({
  selector: 'app-schedule-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeader,
    DataTable,
    TableCell,
    StatusPill,
    JobProgress,
    LoadingState,
    EmptyState,
    ErrorState,
  ],
  templateUrl: './schedule.page.html',
  styleUrl: './schedule.page.scss',
})
export class SchedulePage {
  private readonly scheduleApi = inject(ScheduleService);
  private readonly ops = inject(OperationsService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly jobs = inject(JobsService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly schedule = resource({
    loader: () => this.scheduleApi.overview({ limit: RECENT_RUNS }),
  });
  protected readonly backups = resource({ loader: () => this.ops.backupJobs() });

  protected readonly jobRows = computed(() =>
    this.schedule.hasValue()
      ? jobRows(this.schedule.value().jobs, this.schedule.value().recent)
      : [],
  );
  protected readonly backupRows = computed(() =>
    this.backups.hasValue() ? this.backups.value().items.map(backupRow) : [],
  );

  protected readonly running = signal<string | null>(null);
  protected readonly backupRun = signal<JobHandle | null>(null);
  protected readonly backingUp = signal(false);

  protected readonly jobKey = (j: JobRow) => j.name;
  protected readonly runKey = (r: ScheduledRunView) => r.id;
  protected readonly backupKey = (b: BackupRow) => b.id;

  protected readonly jobColumns: TableColumn<JobRow>[] = [
    { key: 'name', label: 'Job', mobile: 'title' },
    { key: 'action', label: 'Action', value: (j) => humanize(j.action) },
    { key: 'trigger', label: 'When', sortable: false },
    { key: 'next_run_at', label: 'Next run', format: 'datetime' },
    { key: 'last_status', label: 'Last run' },
    { key: 'run', label: 'Run now', sortable: false, value: () => '' },
  ];

  protected readonly runColumns: TableColumn<ScheduledRunView>[] = [
    { key: 'job_name', label: 'Job', mobile: 'title' },
    { key: 'status', label: 'Status' },
    { key: 'as_of', label: 'For', format: 'date' },
    { key: 'started_at', label: 'Started', format: 'datetime' },
    { key: 'finished_at', label: 'Finished', format: 'datetime', mobile: 'hide' },
    {
      key: 'catch_up',
      label: 'Catch-up',
      value: (r) => (r.catch_up ? 'Yes' : 'No'),
      mobile: 'hide',
    },
    { key: 'error', label: 'Error', sortable: false, value: (r) => r.error ?? '' },
  ];

  protected readonly backupColumns: TableColumn<BackupRow>[] = [
    { key: 'backup_id', label: 'Backup', value: (b) => b.backup_id ?? 'None', mobile: 'title' },
    { key: 'status', label: 'Status' },
    { key: 'created_at', label: 'Started', format: 'datetime' },
    { key: 'finished_at', label: 'Finished', format: 'datetime', mobile: 'hide' },
    { key: 'pruned', label: 'Pruned', format: 'number' },
    { key: 'error', label: 'Error', sortable: false, value: (b) => b.error ?? '' },
  ];

  async runNow(job: JobRow): Promise<void> {
    const isTick = job.action === 'tick';
    const ok = await this.confirm.confirm({
      title: `Run ${job.name} now?`,
      message: isTick
        ? 'Active strategies decide and place orders through the broker, outside the schedule.'
        : `Starts ${humanize(job.action).toLowerCase()} in the background, outside the schedule.`,
      confirmLabel: 'Run now',
      tone: isTick ? 'danger' : 'default',
      typedConfirmation: isTick ? job.name : undefined,
    });
    if (!ok) return;
    this.running.set(job.name);
    try {
      const started = await this.scheduleApi.runNow(job.name);
      this.toasts.success(`Started ${job.name} for ${started.as_of}.`);
      this.schedule.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.running.set(null);
    }
  }

  async backUpNow(): Promise<void> {
    if (this.backingUp()) return;
    const ok = await this.confirm.confirm({
      title: 'Back up now?',
      message:
        'Copies the state database, the lake and artifacts to the backup folder, then verifies and prunes old backups. Ingest waits while it runs.',
      confirmLabel: 'Back up now',
    });
    if (!ok) return;
    this.backingUp.set(true);
    try {
      const job = await this.ops.startBackup();
      this.backupRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.backupRun.set(handle);
      this.backups.reload();
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const result = await this.ops.backupResult(job.id);
        this.toasts.success(`Backed up as ${result.backup_id}.`);
      }
      this.backups.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.backingUp.set(false);
    }
  }
}
