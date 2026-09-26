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

  it('lists backup jobs', async () => {
    const jobs = ops.backupJobs(5);
    const req = await nextRequest(controller, '/api/jobs');
    expect(req.request.urlWithParams).toContain('kind=backup');
    expect(req.request.urlWithParams).toContain('limit=5');
    req.flush({ items: [], total: 0, limit: 5, offset: 0 });
    expect((await jobs).total).toBe(0);
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
