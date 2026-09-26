import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type {
  BrokerInfo,
  CostModelPreset,
  DataSourceInfo,
  MeView,
  RiskPolicy,
} from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { AuthTokenService } from '../../core/auth/auth-token.service';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { SettingsPage } from './settings.page';

const SECRET = 'sk-test-7f3a9c2e1d';

const BROKER: BrokerInfo = {
  kind: 'simulated',
  paper: true,
  allow_live: false,
  credentials_configured: false,
};
const RISK: RiskPolicy = {
  enabled: true,
  max_weight_per_ticker: 0.25,
  max_open_positions: 10,
  cash_buffer_fraction: 0.05,
  min_order_notional: 100,
  max_weight_per_asset_class: { crypto: 0.1 },
};
const SOURCES: DataSourceInfo[] = [
  { id: 'eodhd', configured: true, default: true, detail: null },
  { id: 'yahoo', configured: false, default: false, detail: 'install the yahoo extra' },
];
const COSTS: CostModelPreset[] = [
  {
    name: 'realistic',
    description: 'Fees and spreads per asset class.',
    settings: {
      default: { fee_bps: 1, half_spread_bps: 2, fee_flat: 0 },
      asset_classes: { crypto: { fee_bps: 10, half_spread_bps: 5, fee_flat: 0 } },
      impact_bps: 5,
      max_impact_bps: 100,
    },
  },
];

describe('SettingsPage', () => {
  let fixture: ComponentFixture<SettingsPage>;
  let http: HttpTestingController;
  let el: HTMLElement;

  /** Sign in as `me`, render the page and answer its reads. */
  async function setup(me: MeView = ADMIN, broker: BrokerInfo = BROKER): Promise<void> {
    const session = TestBed.inject(SessionService);
    const loading = session.load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(SettingsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    if (me.role === 'admin') {
      (await nextRequest(http, '/api/brokers')).flush(broker);
      (await nextRequest(http, '/api/risk/policy')).flush(RISK);
      (await nextRequest(http, '/api/sources')).flush(SOURCES);
      (await nextRequest(http, '/api/lab/cost-models')).flush(COSTS);
    }
    (await nextRequest(http, '/api/notifications/preferences')).flush({
      channels: ['inapp'],
      preferences: [],
      quiet_start: null,
      quiet_end: null,
      timezone: 'UTC',
      webhook: null,
    });
    await tick();
    fixture.detectChanges();
  }

  function button(text: string): HTMLButtonElement {
    const found = [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
    if (!found) throw new Error(`no "${text}" button`);
    return found;
  }

  function typeToken(value: string): void {
    const input = el.querySelector<HTMLInputElement>('#api-token')!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  function headings(): string[] {
    return [...el.querySelectorAll('h2')].map((h) => h.textContent?.trim() ?? '');
  }

  it('shows a trader their account sections only', async () => {
    await setup(TRADER);
    const h = headings();
    expect(h).toContain('Your account');
    expect(h).toContain('API token');
    expect(h).toContain('Theme');
    expect(h).not.toContain('System');
    expect(h).not.toContain('Broker');
    expect(h).not.toContain('Risk policy');
    expect(h).not.toContain('Data sources');
    expect(h).not.toContain('Cost-model presets');
    expect(el.querySelector('a[href="/profile"]')?.textContent).toContain('Open profile');
    expect(el.textContent).not.toContain('Reload system settings');
    // No system reads for a trader.
    http.expectNone('/api/brokers');
    http.expectNone('/api/risk/policy');
    http.verify();
  });

  it('shows an admin the System sections too', async () => {
    await setup(ADMIN);
    const h = headings();
    expect(h).toContain('Your account');
    expect(h).toContain('System');
    expect(h).toContain('Broker');
    expect(h).toContain('Risk policy');
    expect(h.filter((x) => x === 'Risk policy')).toHaveLength(1);
  });

  it('shows the broker, risk policy, data sources and cost presets', async () => {
    await setup();
    const text = el.textContent ?? '';
    expect(text).not.toContain('[production');
    expect(text).toContain('Simulated');
    expect(text).toContain('Paper');
    expect(text).toContain('25.0%');
    expect(text).toContain('Max weight, crypto');
    expect(text).toContain('EODHD');
    expect(text).toContain('install the yahoo extra');
    expect(text).toContain('realistic');
    expect(text).toContain('Crypto');
    // Alpaca status is only asked for when the broker is Alpaca.
    expect(text).not.toContain('Alpaca connection');
    http.verify();
  });

  it('checks the Alpaca connection only when the broker is Alpaca', async () => {
    await setup(ADMIN, { ...BROKER, kind: 'alpaca', credentials_configured: true });
    (await nextRequest(http, '/api/brokers/alpaca/status')).flush({
      connected: false,
      paper: true,
      error: 'missing API keys',
      account: null,
      clock: null,
    });
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Not connected');
    expect(el.textContent).toContain('missing API keys');
  });

  it('keeps the token masked and in sessionStorage only', async () => {
    await setup(TRADER);
    const input = el.querySelector<HTMLInputElement>('#api-token')!;
    expect(input.type).toBe('password');
    typeToken(SECRET);
    button('Save token').click();
    fixture.detectChanges();

    expect(TestBed.inject(AuthTokenService).token()).toBe(SECRET);
    expect(sessionStorage.getItem('stonks.apiToken')).toBe(SECRET);
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
    expect(input.value).toBe('');
  });

  it('never logs, toasts or renders the token while saving and testing it', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(() => undefined),
    );
    const toasts = TestBed.inject(ToastService);
    await setup(TRADER);

    typeToken(SECRET);
    button('Save token').click();
    fixture.detectChanges();
    button('Test token').click();

    const probe = await nextRequest(http, '/api/auth/check', 'GET');
    expect(probe.request.headers.get('Authorization')).toBe(`Bearer ${SECRET}`);
    expect(probe.request.urlWithParams).not.toContain(SECRET);
    probe.flush({ authenticated: true });
    await tick();
    fixture.detectChanges();

    expect(el.textContent).toContain('Accepted');
    expect(el.textContent).not.toContain(SECRET);
    for (const spy of spies) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(SECRET);
    }
    expect(JSON.stringify(toasts.toasts())).not.toContain(SECRET);
  });

  it('reports a rejected token without a toast', async () => {
    const toasts = TestBed.inject(ToastService);
    await setup(TRADER);
    typeToken('wrong');
    button('Save token').click();
    fixture.detectChanges();
    const before = toasts.toasts().length;
    button('Test token').click();

    (await nextRequest(http, '/api/auth/check', 'GET')).flush(
      { title: 'Unauthorized', status: 401, detail: 'missing or invalid bearer token' },
      { status: 401, statusText: 'Unauthorized' },
    );
    await tick();
    fixture.detectChanges();

    expect(el.textContent).toContain('Rejected');
    expect(toasts.toasts().length).toBe(before);
  });

  it('explains that a token cannot be verified when none is saved', async () => {
    await setup(TRADER);
    button('Test token').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Save a token first');
    http.verify();
  });
});
