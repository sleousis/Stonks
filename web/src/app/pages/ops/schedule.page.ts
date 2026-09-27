import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type {
  BackupView,
  RestoreResultView,
  ScheduledJobView,
  ScheduledRunView,
  VerifyView,
} from '../../api/models';
import { OperationsService, restoreConfirmation } from '../../api/operations.service';
import { ScheduleService } from '../../api/schedule.service';
import { SystemService } from '../../api/system.service';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { type ConfirmOptions, ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate, formatDateTime, formatNumber } from '../../core/format/format';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { TRADING_RUN_ACTION, jobLabel } from '../../core/schedule/job-labels';
import { UpdatedAgo, autoRefresh } from '../../shared/auto-refresh';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { JobProgress, JobResult } from '../../shared/ui/job-progress';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { countdown } from '../../shared/ui/session-strip';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { tickTicket } from '../orders/tick-confirm';

const RECENT_RUNS = 30;
/** How long after a job's next run the page reloads, so the scheduler has fired it. */
export const DUE_GRACE_MS = 2_000;

/** A scheduled job with the status of its latest run. */
export interface JobRow extends ScheduledJobView {
  last_status: string | null;
  last_run_at: string | null;
}

/** A size in bytes as B, KB, MB or GB (1024 steps). */
export function formatSize(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${formatNumber(value, { digits: unit === 0 ? 0 : 1 })} ${units[unit]}`;
}

export function jobRows(jobs: readonly ScheduledJobView[], recent: readonly ScheduledRunView[]) {
  return jobs.map<JobRow>((j) => {
    // `recent` comes newest first.
    const last = recent.find((r) => r.job_name === j.name);
    return { ...j, last_status: last?.status ?? null, last_run_at: last?.started_at ?? null };
  });
}

/** The earliest next run that has passed (with the grace), or null. */
export function passedRun(jobs: readonly ScheduledJobView[], now: number): string | null {
  let best: string | null = null;
  for (const j of jobs) {
    const at = j.next_run_at ? Date.parse(j.next_run_at) : NaN;
    if (Number.isFinite(at) && at + DUE_GRACE_MS <= now && (!best || j.next_run_at! < best)) {
      best = j.next_run_at!;
    }
  }
  return best;
}

/**
 * The built-in scheduler (jobs, next fire, recent runs, run now) and
 * server-side backups: every backup on disk, Back up now, Verify and a
 * staged Restore (a new folder, the live data is never touched). Run now on
 * the trading run is an order ticket with the broker's PAPER or LIVE stamp.
 * The page reloads every minute and just after a job's next run passes.
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
    PermissionNote,
    UpdatedAgo,
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
  private readonly stepUp = inject(StepUpService);
  private readonly system = inject(SystemService);
  protected readonly session = inject(SessionService);

  /** Running jobs by hand is the admins'. */
  protected readonly canRun = computed(() => this.session.can('operations.run'));
  /** Backups cover every portfolio: admins only. */
  protected readonly canBackUp = computed(() => this.session.can('operations.run'));
  /** A restore also needs a fresh second factor, asked for when it starts. */
  protected readonly canRestore = computed(() => this.session.can('backups.restore'));

  /** Ticks every second so "Next run" counts down. */
  protected readonly now = signal(Date.now());

  protected readonly schedule = resource({
    loader: () => this.scheduleApi.overview({ limit: RECENT_RUNS }),
  });
  /** Only admins may list backups, so nobody else asks. */
  protected readonly backups = resource({
    params: () => (this.canBackUp() ? {} : undefined),
    loader: () => this.ops.backups(),
  });
  /** The broker a trading run sends to: the Run now ticket names it and stamps it. */
  protected readonly broker = resource({
    params: () => (this.canRun() ? {} : undefined),
    loader: () => this.system.broker(),
  });
  protected readonly auto = autoRefresh(() => [this.schedule, this.backups]);
  /** The passed next run the page already reloaded for. */
  private reloadedFor: string | null = null;

  protected readonly jobRows = computed(() =>
    this.schedule.hasValue()
      ? jobRows(this.schedule.value().jobs, this.schedule.value().recent)
      : [],
  );
  protected readonly backupRows = computed<BackupView[]>(() =>
    this.backups.hasValue() ? this.backups.value().items : [],
  );

  protected readonly running = signal<string | null>(null);
  protected readonly backupRun = signal<JobHandle | null>(null);
  protected readonly backingUp = signal(false);
  /** The backup being checked or restored (its buttons wait). */
  protected readonly busyBackup = signal<string | null>(null);
  protected readonly verified = signal<VerifyView | null>(null);
  protected readonly restoreRun = signal<JobHandle | null>(null);
  /** Where the restore went; a failed read offers Try again. */
  protected readonly restored = new JobResult<RestoreResultView>();
  protected readonly backedUp = new JobResult<unknown>();

  protected readonly jobKey = (j: JobRow) => j.name;
  protected readonly runKey = (r: ScheduledRunView) => r.id;
  protected readonly backupKey = (b: BackupView) => b.id;

  private readonly baseJobColumns: TableColumn<JobRow>[] = [
    { key: 'name', label: 'Job', value: (j) => jobLabel(j), mobile: 'title' },
    {
      key: 'trigger',
      label: 'When',
      sortable: false,
      value: (j) => j.trigger_text || j.trigger,
    },
    { key: 'next_run_at', label: 'Next run' },
    { key: 'last_status', label: 'Last run' },
  ];
  /** Run now only for those who may use it. */
  protected readonly jobColumns = computed<TableColumn<JobRow>[]>(() =>
    this.canRun()
      ? [...this.baseJobColumns, { key: 'run', label: 'Run now', sortable: false, value: () => '' }]
      : this.baseJobColumns,
  );

  protected readonly jobName = (j: JobRow) => jobLabel(j);
  protected readonly when = (iso: string | null | undefined) => formatDateTime(iso);
  protected untilNext(iso: string | null | undefined): string | null {
    if (!iso) return null;
    const left = countdown(Date.parse(iso) - this.now());
    return left === 'now' ? 'due now' : `in ${left}`;
  }

  constructor() {
    const timer = setInterval(() => {
      this.now.set(Date.now());
      this.reloadIfDue();
    }, 1000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  protected readonly runColumns: TableColumn<ScheduledRunView>[] = [
    {
      key: 'job_name',
      label: 'Job',
      value: (r) => jobLabel({ action: r.action, name: r.job_name }),
      mobile: 'title',
    },
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

  protected readonly backupColumns: TableColumn<BackupView>[] = [
    {
      key: 'created_at',
      label: 'Backup',
      value: (b) => formatDateTime(b.created_at),
      mobile: 'title',
    },
    { key: 'id', label: 'Name', sortable: false, mobile: 'hide' },
    { key: 'size_bytes', label: 'Size', value: (b) => formatSize(b.size_bytes) },
    { key: 'actions', label: 'Actions', sortable: false, value: () => '' },
  ];

  /** A job's next run passed: reload once so the list moves on from "due now". */
  private reloadIfDue(): void {
    if (!this.schedule.hasValue() || this.schedule.isLoading()) return;
    const passed = passedRun(this.schedule.value().jobs, Date.now());
    if (passed && passed !== this.reloadedFor) {
      this.reloadedFor = passed;
      this.schedule.reload();
    }
  }

  /** What Run now asks: an order ticket for the trading run, a plain confirm otherwise. */
  private runNowOptions(job: JobRow): ConfirmOptions | null {
    const label = jobLabel(job);
    if (job.action !== TRADING_RUN_ACTION) {
      return {
        title: `Run the ${label.toLowerCase()} now?`,
        message: `Starts the ${label.toLowerCase()} in the background, outside the schedule.`,
        confirmLabel: 'Run now',
      };
    }
    if (!this.broker.hasValue()) return null;
    const ticket = tickTicket(
      this.broker.value(),
      { asOf: '', tickers: '' },
      'Run the trading run now?',
    );
    return {
      ...ticket,
      message: `Runs now, outside the schedule. ${ticket.message}`,
      cancelLabel: 'Cancel',
    };
  }

  async runNow(job: JobRow): Promise<void> {
    if (!this.canRun()) return;
    const label = jobLabel(job);
    const options = this.runNowOptions(job);
    if (!options) {
      this.toasts.error(
        'Could not read the broker, so the trading run cannot be confirmed. Refresh and try again.',
      );
      return;
    }
    if (!(await this.confirm.confirm(options))) return;
    this.running.set(job.name);
    try {
      const started = await this.scheduleApi.runNow(job.name);
      this.toasts.success(`Started the ${label.toLowerCase()} for ${formatDate(started.as_of)}.`);
      this.schedule.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.running.set(null);
    }
  }

  async verify(backup: BackupView): Promise<void> {
    if (this.busyBackup() || !this.canBackUp()) return;
    this.busyBackup.set(backup.id);
    this.verified.set(null);
    try {
      const result = await this.ops.verifyBackup(backup.id);
      this.verified.set(result);
      if (result.ok)
        this.toasts.success(`Verified the backup from ${this.when(backup.created_at)}.`);
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busyBackup.set(null);
    }
  }

  async restore(backup: BackupView): Promise<void> {
    if (this.busyBackup() || !this.canRestore()) return;
    const ok = await this.confirm.confirm({
      title: `Restore the backup from ${this.when(backup.created_at)}?`,
      message:
        'Copies this backup into a new folder on the server and checks it. The live data is never touched. Switching to the restored copy is a separate step, explained when it finishes.',
      confirmLabel: 'Restore',
      tone: 'danger',
      typedConfirmation: restoreConfirmation(backup.id),
    });
    if (!ok) return;
    if (!(await this.stepUp.ensure('Restore a backup'))) return;
    this.busyBackup.set(backup.id);
    this.restored.reset();
    try {
      // A stale second factor comes back as 403 step_up_required: the session
      // interceptor prompts for a code and retries once.
      const job = await this.ops.restoreBackup(backup.id);
      this.restoreRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.restoreRun.set(handle);
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const view = await this.restored.load(() => this.ops.restoreResult(job.id));
        if (view) this.toasts.success('Restored the backup into a new folder.');
      }
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busyBackup.set(null);
    }
  }

  async backUpNow(): Promise<void> {
    if (this.backingUp() || !this.canBackUp()) return;
    const ok = await this.confirm.confirm({
      title: 'Back up now?',
      message:
        'Copies the trading records, our price data and saved strategies to the backup folder, then checks the copy and removes old backups. Data updates wait while it runs.',
      confirmLabel: 'Back up now',
    });
    if (!ok) return;
    this.backingUp.set(true);
    this.backedUp.reset();
    try {
      const job = await this.ops.startBackup();
      this.backupRun()?.stop();
      const handle = this.jobs.track(job.id, this.destroyRef);
      this.backupRun.set(handle);
      this.backups.reload();
      const last = await handle.finished;
      if (last?.status === 'succeeded') {
        const view = await this.backedUp.load(() => this.ops.backupResult(job.id));
        if (view) this.toasts.success('Backed up the system.');
      }
      this.backups.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.backingUp.set(false);
    }
  }
}
