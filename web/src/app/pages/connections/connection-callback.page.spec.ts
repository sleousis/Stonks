import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { ConnectionCallbackPage } from './connection-callback.page';
import { SNAPTRADE, account, connection } from './connections.fixtures';

describe('ConnectionCallbackPage', () => {
  let fixture: ComponentFixture<ConnectionCallbackPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function setUp(query: Record<string, string>): void {
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { queryParamMap: convertToParamMap(query) } },
        },
        {
          provide: PortfolioContextService,
          useValue: { load: vi.fn().mockResolvedValue(undefined), options: signal([]) },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ConnectionCallbackPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  afterEach(() => http.verify());

  it('completes the connection with the returned parameters and lists its accounts', async () => {
    setUp({ connection_id: 'con_2', state: 'st_1', status: 'SUCCESS' });
    const req = await nextRequest(http, '/api/connections/callback');
    const url = req.request.urlWithParams;
    expect(url).toContain('connection_id=con_2');
    expect(url).toContain('state=st_1');
    expect(url).toContain('status=SUCCESS');
    req.flush(connection({ id: 'con_2', provider: 'snaptrade' }));
    await tick();
    (await nextRequest(http, '/api/connections/con_2/accounts')).flush(
      page([account({ connection_id: 'con_2', name: 'Brokerage', portfolio_id: 'pf_9' })]),
    );
    (await nextRequest(http, '/api/connections/providers')).flush([SNAPTRADE]);
    await settle();
    expect(el.textContent).toContain('SnapTrade is connected.');
    expect(el.querySelector('.account')?.textContent).toContain('Brokerage');
    const open = [...el.querySelectorAll('a')].find((a) => a.textContent?.includes('Open'));
    expect(open?.getAttribute('href')).toBe('/connections/con_2');
  });

  it('says the provider did not finish when the connection stays pending', async () => {
    setUp({ connection_id: 'con_2', state: 'st_1', status: 'ABANDONED' });
    (await nextRequest(http, '/api/connections/callback')).flush(
      connection({
        id: 'con_2',
        status: 'pending',
        last_error: 'the connection was not completed at the provider',
      }),
    );
    await tick();
    (await nextRequest(http, '/api/connections/con_2/accounts')).flush(page([]));
    (await nextRequest(http, '/api/connections/providers')).flush([]);
    await settle();
    expect(el.textContent).toContain('Waiting to finish');
    expect(el.textContent).toContain('Start connecting again');
  });

  it('shows the error and a way back when the callback is refused', async () => {
    setUp({ connection_id: 'con_2', state: 'old' });
    (await nextRequest(http, '/api/connections/callback')).flush(
      { title: 'Conflict', status: 409, detail: 'invalid callback state; start connecting again' },
      { status: 409, statusText: 'Conflict' },
    );
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('invalid callback state');
    const back = [...el.querySelectorAll('a')].find((a) => a.textContent?.includes('Back'));
    expect(back?.getAttribute('href')).toBe('/connections');
    expect(el.textContent).not.toContain('Try again');
  });

  it('explains a return link with missing details without calling the API', async () => {
    setUp({});
    await settle();
    expect(el.textContent).toContain('missing its details');
  });
});
