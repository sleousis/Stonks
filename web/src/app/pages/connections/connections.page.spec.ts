import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { ConnectionView, ProviderView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { BROWSER_REDIRECT } from './browser-redirect';
import { ConnectionsPage } from './connections.page';
import { ALPACA, SNAPTRADE, account, connection, sessionStub } from './connections.fixtures';

describe('ConnectionsPage', () => {
  let fixture: ComponentFixture<ConnectionsPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  let go: ReturnType<typeof vi.fn>;

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  }

  function type(selector: string, value: string): void {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  async function setUp(
    opts: { allowed?: boolean; providers?: ProviderView[]; connections?: ConnectionView[] } = {},
  ): Promise<void> {
    confirm = vi.fn().mockResolvedValue(true);
    go = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfirmService, useValue: { confirm } },
        { provide: SessionService, useValue: sessionStub(opts.allowed ?? true) },
        {
          provide: BROWSER_REDIRECT,
          useValue: { origin: () => 'https://stonks.test', go },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ConnectionsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    const list = opts.connections ?? [];
    (await nextRequest(http, '/api/connections/providers')).flush(
      opts.providers ?? [ALPACA, SNAPTRADE],
    );
    (await nextRequest(http, '/api/connections')).flush(list);
    for (const c of list) {
      (await nextRequest(http, `/api/connections/${c.id}/accounts`)).flush([
        account(),
        account({ external_account_id: 'acc_2' }),
      ]);
    }
    await settle();
  }

  afterEach(() => http.verify());

  it('shows each provider with what it gives and how you connect', async () => {
    await setUp();
    const cards = el.querySelectorAll('.provider');
    expect(cards.length).toBe(2);
    expect(cards[0].textContent).toContain('Alpaca');
    expect(cards[0].textContent).toContain('Reads positions, cash and activity.');
    expect(cards[0].textContent).toContain('API keys');
    expect(cards[1].textContent).toContain('sign in on the SnapTrade site');
    expect(button('Connect with keys')).toBeDefined();
    expect(button('Sign in at SnapTrade')).toBeDefined();
  });

  it('explains the empty state and offers to connect', async () => {
    await setUp();
    expect(el.textContent).toContain('No broker connected yet');
    expect(el.textContent).toContain('positions, cash and activity');
    expect(button('Connect a broker')).toBeDefined();
  });

  it('says an admin must turn brokers on when none are listed', async () => {
    await setUp({ providers: [] });
    expect(el.textContent).toContain('No brokers are turned on yet');
    expect(el.textContent).toContain('An admin must turn on a broker');
  });

  it('lists connections with status, last sync and account count', async () => {
    await setUp({
      connections: [connection({ last_sync_at: new Date().toISOString(), label: 'Main' })],
    });
    const row = el.querySelector('.connection')!;
    expect(row.getAttribute('href')).toBe('/connections/con_1');
    expect(row.textContent).toContain('Alpaca');
    expect(row.textContent).toContain('Main');
    expect(row.textContent).toContain('Connected');
    expect(row.querySelector('.num')?.textContent?.trim()).toBe('2');
    expect(el.textContent).not.toContain('No broker connected yet');
  });

  it('posts the typed keys as password fields, with the paper flag, then opens the connection', async () => {
    await setUp();
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    button('Connect with keys')!.click();
    fixture.detectChanges();

    const inputs = el.querySelectorAll<HTMLInputElement>('form.keys input[type="password"]');
    expect(inputs.length).toBe(2);
    expect(el.querySelector('label[for="key-alpaca-api_key"]')?.textContent).toContain('API key');

    // Nothing is sent until every key is filled in.
    button('Connect')!.click();
    await settle();
    expect(confirm).not.toHaveBeenCalled();
    expect(el.textContent).toContain('Enter the secret key.');

    type('#key-alpaca-api_key', ' AK1 ');
    type('#key-alpaca-secret_key', 'SK1');
    button('Connect')!.click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Connect Alpaca?' }));

    const req = await nextRequest(http, '/api/connections/keys', 'POST');
    expect(req.request.body).toEqual({
      provider: 'alpaca',
      fields: { api_key: 'AK1', secret_key: 'SK1', paper: 'true' },
      label: null,
    });
    req.flush(connection({ id: 'con_9' }));
    await settle();
    expect(success).toHaveBeenCalledWith('Connected Alpaca.');
    expect(navigate).toHaveBeenCalledWith(['/connections', 'con_9']);
    // The form closed and the keys are gone from the page.
    expect(el.querySelector('form.keys')).toBeNull();
  });

  it('starts the hosted sign-in and sends the browser to its URL', async () => {
    await setUp();
    button('Sign in at SnapTrade')!.click();
    await settle();
    expect(confirm).toHaveBeenCalled();
    const req = await nextRequest(http, '/api/connections/portal', 'POST');
    expect(req.request.body).toEqual({
      provider: 'snaptrade',
      redirect_uri: 'https://stonks.test/connections/callback',
    });
    req.flush({
      connection_id: 'con_2',
      url: 'https://app.snaptrade.test/login?x=1',
      expires_at: '2026-09-26T10:00:00Z',
    });
    await settle();
    expect(go).toHaveBeenCalledWith('https://app.snaptrade.test/login?x=1');
  });

  it('does nothing when the trader cancels the hosted sign-in', async () => {
    await setUp();
    confirm.mockResolvedValue(false);
    button('Sign in at SnapTrade')!.click();
    await settle();
    expect(http.match('/api/connections/portal')).toEqual([]);
    expect(go).not.toHaveBeenCalled();
  });

  it('shows a viewer no connect buttons and says why', async () => {
    await setUp({ allowed: false });
    expect(button('Connect with keys')).toBeUndefined();
    expect(button('Sign in at SnapTrade')).toBeUndefined();
    expect(button('Connect a broker')).toBeUndefined();
    expect(el.textContent).toContain('Traders only.');
  });
});
