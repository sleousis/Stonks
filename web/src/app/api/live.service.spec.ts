import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { ApiError } from '../core/http/api-error';
import { LiveService } from './live.service';
import { provideApi } from './provide-api';

describe('LiveService', () => {
  let controller: HttpTestingController;
  let live: LiveService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    live = TestBed.inject(LiveService);
  });

  afterEach(() => controller.verify());

  it('reads a missing account profile as null', async () => {
    const got = live.profile('pf_live');
    const req = await nextRequest(controller, '/api/portfolios/pf_live/live/account-profile');
    req.flush(
      { title: 'Not Found', status: 404, detail: 'no account profile' },
      { status: 404, statusText: 'Not Found' },
    );
    expect(await got).toBeNull();
  });

  it('still throws other profile errors', async () => {
    const got = live.profile('pf_live');
    const req = await nextRequest(controller, '/api/portfolios/pf_live/live/account-profile');
    req.flush({ detail: 'boom' }, { status: 500, statusText: 'Server Error' });
    await expect(got).rejects.toBeInstanceOf(ApiError);
  });

  it('sets the allocation with its reason', async () => {
    const saved = live.setAllocation('pf_live', {
      amount: 2500,
      currency: 'USD',
      reason: 'first slice',
    });
    const req = await nextRequest(controller, '/api/portfolios/pf_live/live/allocation', 'PUT');
    expect(req.request.body).toEqual({ amount: 2500, currency: 'USD', reason: 'first slice' });
    req.flush({
      portfolio_id: 'pf_live',
      amount: 2500,
      currency: 'USD',
      reason: 'first slice',
      updated_at: '2026-09-27T12:00:00Z',
      updated_by: 'user:u1',
    });
    expect((await saved).amount).toBe(2500);
  });

  it('reads the live rules and the broker gateways', async () => {
    const rules = live.rules('pf_live');
    (await nextRequest(controller, '/api/portfolios/pf_live/live/rules')).flush({
      portfolio_id: 'pf_live',
      safeguards: [],
      account_rules_on: false,
      profile_set: false,
      account_rules: [],
    });
    expect((await rules).profile_set).toBe(false);

    const gateways = live.gateways();
    (await nextRequest(controller, '/api/brokers/gateways')).flush({
      configured: false,
      gateways: [],
    });
    expect((await gateways).configured).toBe(false);
  });
});
