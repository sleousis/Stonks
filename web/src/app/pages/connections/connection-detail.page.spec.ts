import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { PortfolioRef } from '../../api/portfolios.service';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { nextRequest, page, tick } from '../../../testing/http';
import { ConnectionDetailPage } from './connection-detail.page';
import { ALPACA, account, connection, sessionStub } from './connections.fixtures';
import { book } from '../../../testing/portfolio-fixtures';

const PORTFOLIOS: PortfolioRef[] = [
  book({ id: 'pf_default', name: 'Main', trading: 'paper', is_default: true }),
  book({ id: 'pf_broker', name: 'Alpaca mirror', trading: 'live' }),
];

describe('ConnectionDetailPage', () => {
  let fixture: ComponentFixture<ConnectionDetailPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  }

  async function setUp(allowed = true): Promise<void> {
    confirm = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: SessionService, useValue: sessionStub(allowed) },
        {
          provide: PortfolioContextService,
          useValue: { load: vi.fn().mockResolvedValue(undefined), options: signal(PORTFOLIOS) },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ConnectionDetailPage);
    fixture.componentRef.setInput('id', 'con_1');
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/connections/providers')).flush([ALPACA]);
    (await nextRequest(http, '/api/connections/con_1')).flush(
      connection({ last_sync_at: new Date().toISOString(), last_sync_status: 'ok' }),
    );
    (await nextRequest(http, '/api/connections/con_1/accounts')).flush(
      page([
        account(),
        account({ external_account_id: 'acc_2', name: 'IRA', portfolio_id: 'pf_broker' }),
      ]),
    );
    await settle();
  }

  afterEach(() => http.verify());

  it('shows the connection, its accounts and the portfolio each feeds', async () => {
    await setUp();
    expect(el.querySelector('h1')?.textContent).toContain('Alpaca');
    expect(el.textContent).toContain('Connected');
    expect(el.textContent).toContain('Synced');
    const accounts = el.querySelectorAll('.account');
    expect(accounts.length).toBe(2);
    expect(accounts[0].textContent).toContain('Not linked to a portfolio');
    expect(accounts[0].textContent).toContain('ending 1234');
    expect(accounts[1].textContent).toContain('Feeds Alpaca mirror');
    expect(accounts[1].querySelector('app-mode-stamp')?.textContent).toContain('LIVE');
  });

  it('says when this broker places orders, never that it only reads (F53)', async () => {
    await setUp();
    const lead = el.querySelector('app-page-header')!.textContent!;
    expect(lead).toContain('places orders there only for Approve each trade and Automatic');
    expect(lead).not.toContain('It never trades there');
  });

  it('links an account to the chosen portfolio', async () => {
    await setUp();
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    const select = el.querySelector<HTMLSelectElement>('#link-acc_1')!;
    select.value = 'pf_broker';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    el.querySelectorAll<HTMLButtonElement>('.account .btn')[0].click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Link Individual?', confirmLabel: 'Link' }),
    );
    const req = await nextRequest(http, '/api/connections/con_1/link', 'POST');
    expect(req.request.body).toEqual({ external_account_id: 'acc_1', portfolio_id: 'pf_broker' });
    req.flush({ connection_id: 'con_1', external_account_id: 'acc_1', portfolio_id: 'pf_broker' });
    await settle();
    expect(success).toHaveBeenCalledWith('Linked Individual to Alpaca mirror.');
    (await nextRequest(http, '/api/connections/con_1/accounts')).flush(
      page([account({ portfolio_id: 'pf_broker' })]),
    );
    await settle();
  });

  it('links to a new portfolio by default (no portfolio id)', async () => {
    await setUp();
    el.querySelectorAll<HTMLButtonElement>('.account .btn')[0].click();
    await settle();
    const req = await nextRequest(http, '/api/connections/con_1/link', 'POST');
    expect(req.request.body).toEqual({ external_account_id: 'acc_1', portfolio_id: null });
    req.flush({ connection_id: 'con_1', external_account_id: 'acc_1', portfolio_id: 'pf_new' });
    await settle();
    (await nextRequest(http, '/api/connections/con_1/accounts')).flush([]);
    await settle();
  });

  it('syncs on request and shows what the sync found', async () => {
    await setUp();
    button('Sync now')!.click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Sync Alpaca now?' }));
    (await nextRequest(http, '/api/connections/con_1/sync', 'POST')).flush({
      connection_id: 'con_1',
      status: 'partial',
      error: null,
      next_sync_at: null,
      accounts_seen: 2,
      portfolios: [
        {
          portfolio_id: 'pf_broker',
          external_account_id: 'acc_2',
          snapshot_id: 7,
          positions: 3,
          unmapped: ['XYZ.TO'],
          activities_new: 1,
          activities_seen: 4,
          error: null,
        },
      ],
    });
    await settle();
    const result = el.querySelector('.sync-result')!;
    expect(result.textContent).toContain('Partly synced');
    expect(result.textContent).toContain('Found 2 accounts');
    expect(result.textContent).toContain('Alpaca mirror');
    expect(result.textContent).toContain('3 positions');
    expect(result.textContent).toContain('1 new activity');
    expect(result.textContent).toContain('Not recognised: XYZ.TO');
    (await nextRequest(http, '/api/connections/con_1')).flush(connection());
    (await nextRequest(http, '/api/connections/con_1/accounts')).flush([account()]);
    await settle();
  });

  it('asks before disconnecting, naming the provider, then goes back to the list', async () => {
    await setUp();
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    confirm.mockResolvedValueOnce(false);
    button('Disconnect')!.click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Disconnect Alpaca?',
        confirmLabel: 'Disconnect Alpaca',
        tone: 'danger',
      }),
    );
    expect(http.match({ method: 'DELETE', url: '/api/connections/con_1' })).toEqual([]);

    button('Disconnect')!.click();
    await settle();
    (await nextRequest(http, '/api/connections/con_1', 'DELETE')).flush({
      connection_id: 'con_1',
      archived_portfolios: ['pf_broker'],
      remote_removed: true,
      remote_error: null,
    });
    await settle();
    expect(navigate).toHaveBeenCalledWith(['/connections']);
  });

  it('disconnect with a live linked account shows LIVE, the portfolio names, and needs typing', async () => {
    await setUp();
    confirm.mockResolvedValueOnce(false);
    button('Disconnect')!.click();
    await settle();
    const options = confirm.mock.calls[0][0];
    expect(options.ticket.live).toBe(true);
    expect(options.typedConfirmation).toBe('Alpaca');
    expect(options.ticket.lines).toContainEqual({ label: 'IRA', value: 'Alpaca mirror (live)' });
    expect(options.message).toContain('Alpaca mirror');
    expect(options.message).toContain('archives');
  });

  it('hides sync and disconnect from a viewer and disables linking', async () => {
    await setUp(false);
    expect(button('Sync now')).toBeUndefined();
    expect(button('Disconnect')).toBeUndefined();
    expect(el.querySelector<HTMLButtonElement>('.account .btn')?.disabled).toBe(true);
    expect(el.textContent).toContain('Traders only.');
  });
});
