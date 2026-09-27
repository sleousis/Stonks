import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { AlertsService } from './alerts.service';
import { provideApi } from './provide-api';

describe('AlertsService', () => {
  let controller: HttpTestingController;
  let alerts: AlertsService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    alerts = TestBed.inject(AlertsService);
  });

  afterEach(() => controller.verify());

  it('reads one page of alerts', async () => {
    const page = alerts.list({ limit: 20, offset: 40 });
    const req = await nextRequest(controller, '/api/alerts');
    expect(req.request.urlWithParams).toContain('limit=20');
    expect(req.request.urlWithParams).toContain('offset=40');
    req.flush({ items: [], total: 0, limit: 20, offset: 40 });
    expect((await page).total).toBe(0);
  });
});
