import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { nextRequest } from '../../testing/http';
import { ConnectionsService } from './connections.service';
import { provideApi } from './provide-api';

describe('ConnectionsService', () => {
  let controller: HttpTestingController;
  let connections: ConnectionsService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    controller = TestBed.inject(HttpTestingController);
    connections = TestBed.inject(ConnectionsService);
  });

  afterEach(() => controller.verify());

  it('sends API keys in the body only', async () => {
    const done = connections.connectWithKeys({ provider: 'alpaca', fields: { api_key: 'k' } });
    const req = await nextRequest(controller, '/api/connections/keys', 'POST');
    expect(req.request.body).toEqual({ provider: 'alpaca', fields: { api_key: 'k' } });
    expect(req.request.urlWithParams).toBe('/api/connections/keys');
    req.flush({ id: 'con_1', provider: 'alpaca', status: 'active' });
    expect((await done).id).toBe('con_1');
  });

  it('completes a portal connection with the callback parameters', async () => {
    const done = connections.completePortal({ connection_id: 'con_1', state: 's1' });
    const req = await nextRequest(controller, '/api/connections/callback');
    expect(req.request.urlWithParams).toContain('connection_id=con_1');
    expect(req.request.urlWithParams).toContain('state=s1');
    req.flush({ id: 'con_1', status: 'active' });
    await done;
  });

  it('syncs and removes by id', async () => {
    const synced = connections.sync('con_1');
    (await nextRequest(controller, '/api/connections/con_1/sync', 'POST')).flush({
      connection_id: 'con_1',
      status: 'ok',
      portfolios: [],
    });
    expect((await synced).status).toBe('ok');
    const removed = connections.remove('con_1');
    (await nextRequest(controller, '/api/connections/con_1', 'DELETE')).flush({
      connection_id: 'con_1',
      archived_portfolios: [],
    });
    await removed;
  });
});
