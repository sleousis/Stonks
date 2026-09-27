import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { BackupView, Job, MeView, Page, ScheduleView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { StepUpService } from '../../core/auth/step-up.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { type JobHandle, JobsService } from '../../core/jobs/jobs.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { SchedulePage, formatSize, jobRows } from './schedule.page';

const SCHEDULE: ScheduleView = {
  backend: 'in_process',
  hosted: true,
  jobs: [
    {
      name: 'tick',
      action: 'tick',
      trigger: 'XNYS close +45m',
      trigger_text: '45 minutes after the New York market closes, on trading days',
      next_run_at: '2026-09-28T20:45:00Z',
      next_as_of: '2026-09-28',
    },
    {
      name: 'health',
      action: 'health',
      trigger: 'every 240m',
      next_run_at: '2026-09-26T16:00:00Z',
      next_as_of: null,
    },
  ],
  recent: [
    {
      id: 'r2',
      job_name: 'tick',
      action: 'tick',
      run_key: '2026-09-25',
      scheduled_for: '2026-09-25T20:45:00Z',
      as_of: '2026-09-25',
      status: 'succeeded',
      catch_up: false,
      started_at: '2026-09-25T20:45:01Z',
      finished_at: '2026-09-25T20:46:00Z',
      detail: null,
      error: null,
    },
    {
      id: 'r1',
      job_name: 'tick',
      action: 'tick',
      run_key: '2026-09-24',
      scheduled_for: '2026-09-24T20:45:00Z',
      as_of: '2026-09-24',
      status: 'failed',
      catch_up: false,
      started_at: '2026-09-24T20:45:01Z',
      finished_at: '2026-09-24T20:45:30Z',
      detail: null,
      error: 'broker down',
    },
  ],
};

function backupJob(over: Partial<Job> = {}): Job {
  return {
    id: 'job_b1',
    kind: 'backup',
    params: {},
    status: 'succeeded',
    progress: 1,
    created_at: '2026-09-25T02:00:00Z',
    finished_at: '2026-09-25T02:03:00Z',
    result: { backup_id: 'stonks-20260925T020000Z', pruned: ['a', 'b'] },
    ...over,
  };
}

const BACKUP: BackupView = {
  id: 'stonks-20260925T020000Z',
  created_at: '2026-09-25T02:00:00Z',
  size_bytes: 5 * 1024 * 1024,
};

function backupPage(items: BackupView[] = [BACKUP]): Page<BackupView> {
  return { items, total: items.length, limit: 50, offset: 0 };
}

function finished(jobId: string): JobHandle {
  const event = { job_id: jobId, status: 'succeeded', progress: 1 } as const;
  return {
    jobId,
    event: signal(event),
    status: signal('succeeded'),
    progress: signal(1),
    message: signal(null),
    error: signal(null),
    done: signal(true),
    finished: Promise.resolve(event),
    stop: () => undefined,
  };
}

describe('schedule helpers', () => {
  it('adds the latest run status to each job', () => {
    const rows = jobRows(SCHEDULE.jobs, SCHEDULE.recent);
    expect(rows[0]).toMatchObject({ name: 'tick', last_status: 'succeeded' });
    expect(rows[1]).toMatchObject({ name: 'health', last_status: null });
  });

  it('writes sizes in bytes, KB, MB and GB', () => {
    expect(formatSize(512)).toBe('512 B');
    expect(formatSize(1536)).toBe('1.5 KB');
    expect(formatSize(5 * 1024 * 1024)).toBe('5 MB');
    expect(formatSize(null)).toBe('–');
  });
});

describe('SchedulePage', () => {
  let fixture: ComponentFixture<SchedulePage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let track: ReturnType<typeof vi.fn>;
  let stepUp: ReturnType<typeof vi.fn>;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  }

  async function setup(me: MeView): Promise<void> {
    confirm = vi.fn().mockResolvedValue(true);
    track = vi.fn((id: string) => finished(id));
    stepUp = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: JobsService, useValue: { track } },
        { provide: StepUpService, useValue: { ensure: stepUp } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const signingIn = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await signingIn;
    fixture = TestBed.createComponent(SchedulePage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    const sched = await nextRequest(http, '/api/schedule');
    expect(sched.request.urlWithParams).toContain('limit=30');
    sched.flush(SCHEDULE);
    if (me.role === 'admin') {
      const list = await nextRequest(http, '/api/backups');
      expect(list.request.urlWithParams).toContain('limit=50');
      list.flush(backupPage());
    }
    await settle();
  }

  afterEach(() => http.verify());

  describe('as an admin', () => {
    beforeEach(() => setup(ADMIN));

    it('shows jobs, recent runs and backups', () => {
      const jobs = el.querySelector('[aria-labelledby="jobs-title"]')!;
      expect(jobs.textContent).toContain('Runs inside the server');
      expect(jobs.textContent).toContain('45 minutes after the New York market closes');
      expect(jobs.textContent).not.toContain('XNYS close +45m');
      expect(jobs.textContent).toContain('Never');
      // Names read as words; the next run shows its time and a countdown.
      expect(jobs.textContent).toContain('Health');
      expect(jobs.querySelector('.next .num')).not.toBeNull();
      expect(jobs.querySelector('.until')!.textContent).toMatch(/in |due now/);
      expect(jobs.textContent).not.toContain('in_process');
      const runs = el.querySelector('[aria-labelledby="runs-title"]')!;
      expect(runs.textContent).toContain('broker down');
      const backups = el.querySelector('[aria-labelledby="backups-title"]')!;
      expect(backups.textContent).toContain('5 MB');
      expect(backups.querySelector('button[aria-label="Verify backup stonks-20260925T020000Z"]'))
        .not.toBeNull();
      expect(el.textContent).not.toContain('command line');
      expect(el.textContent).not.toContain('[scheduler]');
    });

    it('runs a tick now only after typing its name', async () => {
      const runButtons = [...el.querySelectorAll<HTMLButtonElement>('button[aria-label^="Run "]')];
      runButtons.find((b) => b.getAttribute('aria-label') === 'Run Tick now')!.click();
      const post = await nextRequest(http, '/api/schedule/tick/run-now', 'POST');
      expect(confirm).toHaveBeenCalledWith(
        expect.objectContaining({ typedConfirmation: 'tick', tone: 'danger' }),
      );
      post.flush({ job: 'tick', run_key: 'manual:x', as_of: '2026-09-26', status: 'started' });
      (await nextRequest(http, '/api/schedule')).flush(SCHEDULE);
      await settle();
    });

    it('backs up now, follows the job and names the backup', async () => {
      const success = vi.spyOn(TestBed.inject(ToastService), 'success');
      button('Back up now')!.click();
      const post = await nextRequest(http, '/api/backups', 'POST');
      post.flush(backupJob({ id: 'job_b2', status: 'queued', result: null }));
      await tick();
      for (const r of http.match((x) => x.url.split('?')[0] === '/api/backups')) {
        r.flush(backupPage());
      }
      (await nextRequest(http, '/api/backups/jobs/job_b2/result')).flush({
        backup_id: 'stonks-20260926T120000Z',
        pruned: [],
      });
      await tick();
      for (const r of http.match((x) => x.url.split('?')[0] === '/api/backups')) {
        r.flush(backupPage([{ ...BACKUP, id: 'stonks-20260926T120000Z' }, BACKUP]));
      }
      await settle();
      expect(track).toHaveBeenCalledWith('job_b2', expect.anything());
      expect(success).toHaveBeenCalledWith('Backed up the system.');
      expect(el.querySelector('app-job-progress')!.textContent).toContain('Backup: Finished.');
    });
  });

  describe('backups on disk, as an admin', () => {
    beforeEach(() => setup(ADMIN));

    it('verifies a backup and says what it found', async () => {
      el.querySelector<HTMLButtonElement>(
        'button[aria-label="Verify backup stonks-20260925T020000Z"]',
      )!.click();
      (await nextRequest(http, `/api/backups/${BACKUP.id}/verify`, 'POST')).flush({
        backup_id: BACKUP.id,
        ok: false,
        problems: ['lake.duckdb: checksum mismatch'],
      });
      await settle();
      const outcome = el.querySelector('.outcome')!;
      expect(outcome.getAttribute('data-ok')).toBe('false');
      expect(outcome.textContent).toContain('checksum mismatch')
      expect(outcome.textContent).toContain('Do not restore from it.');
    });

    it('restores only after the typed words and a step-up, and never touches live data', async () => {
      const success = vi.spyOn(TestBed.inject(ToastService), 'success');
      el.querySelector<HTMLButtonElement>(
        'button[aria-label="Restore backup stonks-20260925T020000Z"]',
      )!.click();
      const post = await nextRequest(http, `/api/backups/${BACKUP.id}/restore`, 'POST');
      expect(confirm).toHaveBeenCalledWith(
        expect.objectContaining({
          tone: 'danger',
          typedConfirmation: `RESTORE ${BACKUP.id}`,
          message: expect.stringContaining('The live data is never touched.'),
        }),
      );
      expect(stepUp).toHaveBeenCalledWith('Restore a backup');
      expect(post.request.body).toEqual({ confirmation: `RESTORE ${BACKUP.id}` });
      post.flush(backupJob({ id: 'job_r', kind: 'backup_restore', status: 'queued' }));
      (await nextRequest(http, '/api/backups/restores/job_r/result')).flush({
        backup_id: BACKUP.id,
        data_dir: '/data/restores/r1',
        next_steps: ['Stop the server.', 'Point the data folder at the restored copy.'],
        lake_migrations_applied: [],
        state_migrations_applied: [],
      });
      await settle();
      expect(track).toHaveBeenCalledWith('job_r', expect.anything());
      const outcome = el.querySelector('.outcome')!;
      expect(outcome.textContent).toContain('/data/restores/r1');
      expect(outcome.textContent).toContain('The live data was not touched.');
      expect(outcome.querySelectorAll('ol li').length).toBe(2);
      expect(success).toHaveBeenCalledWith('Restored the backup into a new folder.');
    });

    it('does nothing when the step-up is cancelled', async () => {
      stepUp.mockResolvedValue(false);
      el.querySelector<HTMLButtonElement>(
        'button[aria-label="Restore backup stonks-20260925T020000Z"]',
      )!.click();
      await settle();
      expect(http.match(`/api/backups/${BACKUP.id}/restore`)).toEqual([]);
    });
  });

  describe('as a trader', () => {
    beforeEach(() => setup(TRADER));

    it('sees the schedule but cannot run jobs or back up', () => {
      const jobs = el.querySelector('[aria-labelledby="jobs-title"]')!;
      expect(jobs.querySelector('button[aria-label^="Run "]')).toBeNull();
      expect(jobs.textContent).toContain('Admins only.');
      const backUp = button('Back up now')!;
      expect(backUp.disabled).toBe(true);
      expect(backUp.parentElement!.textContent).toContain('Admins only.');
      expect(el.textContent).toContain('Backups are for admins');
    });
  });
});
