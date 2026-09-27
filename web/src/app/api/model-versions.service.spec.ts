import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { CANDIDATE_V2, LIVE_V1, MODEL_ID } from '../../testing/model-version-fixtures';
import { nextRequest, page } from '../../testing/http';
import { ModelVersionsService } from './model-versions.service';
import { provideApi } from './provide-api';

describe('ModelVersionsService', () => {
  let api: ModelVersionsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    api = TestBed.inject(ModelVersionsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists the versions of a strategy', async () => {
    const listing = api.list(MODEL_ID);
    (await nextRequest(http, `/api/strategies/${MODEL_ID}/versions`)).flush(
      page([LIVE_V1, CANDIDATE_V2]),
    );
    expect((await listing).map((v) => v.version)).toEqual([1, 2]);
  });

  it('swaps with the override and its reason', async () => {
    const swapping = api.swap(MODEL_ID, 2, {
      reason: 'the refit handles the new regime',
      override: true,
    });
    const req = await nextRequest(http, `/api/strategies/${MODEL_ID}/versions/2/swap`, 'POST');
    expect(req.request.body).toEqual({
      reason: 'the refit handles the new regime',
      override: true,
    });
    req.flush({ ...CANDIDATE_V2, status: 'live' });
    expect((await swapping).status).toBe('live');
  });

  it('rejects with a reason', async () => {
    const rejecting = api.reject(MODEL_ID, 2, 'worse fit');
    const req = await nextRequest(http, `/api/strategies/${MODEL_ID}/versions/2/reject`, 'POST');
    expect(req.request.body).toEqual({ reason: 'worse fit' });
    req.flush({ ...CANDIDATE_V2, status: 'rejected' });
    await rejecting;
  });

  it('starts a retrain of one strategy', async () => {
    const starting = api.retrain({ strategy_ids: [MODEL_ID], force: true });
    const req = await nextRequest(http, '/api/model-versions/retrain', 'POST');
    expect(req.request.body).toEqual({ strategy_ids: [MODEL_ID], force: true });
    req.flush({ id: 'job_r1', kind: 'model_retrain', params: {}, status: 'queued', progress: 0 });
    expect((await starting).id).toBe('job_r1');
  });
});
