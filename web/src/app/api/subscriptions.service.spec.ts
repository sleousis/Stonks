import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { ApiError } from '../core/http/api-error';
import { nextRequest } from '../../testing/http';
import { provideApi } from './provide-api';
import { SubscriptionsService } from './subscriptions.service';

describe('SubscriptionsService', () => {
  let svc: SubscriptionsService;
  let controller: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    svc = TestBed.inject(SubscriptionsService);
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  it('lists the caller subscriptions', async () => {
    const result = svc.list();
    const req = await nextRequest(controller, '/api/subscriptions');
    expect(req.request.urlWithParams).toContain('limit=500');
    req.flush({ items: [], total: 0, limit: 500, offset: 0 });
    expect(await result).toEqual([]);
  });

  it('patches the mode or the switch', async () => {
    const result = svc.update('sub 1', { mode: 'paper' });
    const req = await nextRequest(controller, '/api/subscriptions/sub%201', 'PATCH');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ mode: 'paper' });
    req.flush({ id: 'sub 1' });
    expect(await result).toEqual({ id: 'sub 1' });
  });

  it('turns failures into ApiErrors', async () => {
    const result = svc.list();
    (await nextRequest(controller, '/api/subscriptions')).flush(
      { title: 'Not Found', status: 404 },
      { status: 404, statusText: 'Not Found' },
    );
    const err = await result.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
  });
});
