import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { OperationsService } from './operations.service';
import { provideApi } from './provide-api';

describe('OperationsService', () => {
  let controller: HttpTestingController;
  let ops: OperationsService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    ops = TestBed.inject(OperationsService);
  });

  afterEach(() => controller.verify());

  it('starts a backup and reads its result', async () => {
    const started = ops.startBackup();
    (await nextRequest(controller, '/api/backups', 'POST')).flush({ id: 'job_b' });
    expect((await started).id).toBe('job_b');

    const result = ops.backupResult('job_b');
    (await nextRequest(controller, '/api/backups/jobs/job_b/result')).flush({
      backup_id: 'stonks-20260926T020000Z',
      pruned: [],
    });
    expect((await result).backup_id).toBe('stonks-20260926T020000Z');
  });

  it('lists the backups on disk', async () => {
    const list = ops.backups(5);
    const req = await nextRequest(controller, '/api/backups');
    expect(req.request.urlWithParams).toContain('limit=5');
    req.flush({ items: [], total: 0, limit: 5, offset: 0 });
    expect((await list).total).toBe(0);
  });

  it('verifies a backup', async () => {
    const verified = ops.verifyBackup('stonks-1');
    (await nextRequest(controller, '/api/backups/stonks-1/verify', 'POST')).flush({
      backup_id: 'stonks-1',
      ok: true,
      problems: [],
    });
    expect((await verified).ok).toBe(true);
  });

  it('restores with the typed confirmation, then reads where the data went', async () => {
    const started = ops.restoreBackup('stonks-1');
    const post = await nextRequest(controller, '/api/backups/stonks-1/restore', 'POST');
    expect(post.request.body).toEqual({ confirmation: 'RESTORE stonks-1' });
    post.flush({ id: 'job_r' });
    expect((await started).id).toBe('job_r');

    const result = ops.restoreResult('job_r');
    (await nextRequest(controller, '/api/backups/restores/job_r/result')).flush({
      backup_id: 'stonks-1',
      data_dir: '/data/restore-1',
      next_steps: ['Stop the server'],
      lake_migrations_applied: [],
      state_migrations_applied: [],
    });
    expect((await result).data_dir).toBe('/data/restore-1');
  });

  it('reads statement flags with filters', async () => {
    const flags = ops.statementFlags({ ticker: 'AAPL.US', severity: 'error', limit: 50 });
    const req = await nextRequest(controller, '/api/statements/flags');
    expect(req.request.urlWithParams).toContain('ticker=AAPL.US');
    expect(req.request.urlWithParams).toContain('severity=error');
    req.flush({ items: [], total: 0, limit: 50, offset: 0 });
    await flags;
  });
});
