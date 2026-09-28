import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
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
import { FeatureFlagsService } from '../../core/features/feature-flags.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { SettingsPage } from './settings.page';
import { provideFakeCalendars } from '../../../testing/fake-calendars';

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
  const assistantOff = signal(false);

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
    await tick();
    fixture.detectChanges();
  }

  /** Open a section by its tab, and answer the reads of the panels it shows. */
  async function open(label: string): Promise<void> {
    const tab = [...el.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
      (t) => t.textContent?.trim() === label,
    );
    if (!tab) throw new Error(`no "${label}" tab`);
    tab.click();
    fixture.detectChanges();
    if (label === 'Alerts') {
      (await nextRequest(http, '/api/notifications/preferences')).flush({
        channels: ['inapp'],
        preferences: [],
        quiet_start: null,
        quiet_end: null,
        timezone: 'UTC',
        webhook: null,
      });
      (await nextRequest(http, '/api/telegram/link')).flush({
        bot_configured: false,
        bot_enabled: false,
        linked: false,
      });
    }
    if (label === 'Risk limits') {
      (await nextRequest(http, '/api/risk/limits')).flush(limitsView({}));
    }
    await tick();
    fixture.detectChanges();
  }

  function tabs(): string[] {
    return [...el.querySelectorAll('[role="tab"]')].map((t) => t.textContent?.trim() ?? '');
  }

  function limitsView(mine: Record<string, number>) {
    return {
      system: RISK,
      mine,
      effective: { ...RISK, ...mine },
      ignored: [],
    };
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
    assistantOff.set(false);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeCalendars(),
        {
          provide: FeatureFlagsService,
          useValue: { on: () => !assistantOff(), assistantOff: assistantOff.asReadonly() },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  function headings(): string[] {
    return [...el.querySelectorAll('h2, h3')].map((h) => h.textContent?.trim() ?? '');
  }

  it('splits Settings into sections, one on screen at a time (M14)', async () => {
    await setup(TRADER);
    expect(tabs()).toEqual(['Account', 'Alerts', 'Display', 'Risk limits']);
    expect(el.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.trim()).toBe(
      'Account',
    );
    const panel = el.querySelector('[role="tabpanel"]')!;
    expect(panel.getAttribute('aria-labelledby')).toBe('settings-tab-account');
    const h = headings();
    expect(h).toContain('Sign-in and security');
    expect(h).toContain('API token');
    expect(h).not.toContain('Theme');
    expect(h).not.toContain('Telegram');
    expect(el.querySelector('a[href="/profile"]')?.textContent).toContain('Open profile');
    // UX-43: the token panel is folded away under For scripts.
    const scripts = el.querySelector<HTMLDetailsElement>('details.scripts')!;
    expect(scripts.querySelector('summary')?.textContent?.trim()).toBe('For scripts');
    expect(scripts.open).toBe(false);
    expect(scripts.contains(el.querySelector('#api-token'))).toBe(true);
    expect(el.textContent).not.toContain('Reload system settings');
    // No system reads for a trader.
    http.expectNone('/api/brokers');
    http.expectNone('/api/risk/policy');
    http.verify();
  });

  it('keeps alerts, display and risk limits each in their own section', async () => {
    await setup(TRADER);
    await open('Alerts');
    expect(headings()).toContain('Telegram');
    expect(headings()).toContain('Alert settings');
    expect(el.querySelector('a[href="/notifications"]')).not.toBeNull();
    await open('Display');
    expect(headings()).toContain('Theme');
    expect(headings()).toContain('Keyboard');
    expect(headings()).not.toContain('Telegram');
    await open('Risk limits');
    expect(headings()).toContain('Your risk limits');
    http.verify();
  });

  it('nests panel headings under a section heading (UX-72)', async () => {
    await setup(ADMIN);
    await open('System');
    const h2 = [...el.querySelectorAll('h2')].map((h) => h.textContent?.trim());
    expect(h2).toEqual(['System']);
    expect(el.querySelector('#broker-title')?.tagName).toBe('H3');
  });

  it("shows the broker's paper or live mode as the stamp (UX-72)", async () => {
    await setup(ADMIN, { ...BROKER, paper: false });
    await open('System');
    const panel = el.querySelector('#broker-title')!.closest('section')!;
    expect(panel.querySelector('app-mode-stamp')).not.toBeNull();
    expect(panel.querySelector('app-status-pill[status="live"]')).toBeNull();
  });

  it('gives an admin the System section too', async () => {
    await setup(ADMIN);
    expect(tabs()).toEqual(['Account', 'Alerts', 'Display', 'Risk limits', 'System']);
    await open('System');
    const h = headings();
    expect(h).toContain('System');
    expect(h).toContain('Broker');
    expect(h).toContain('Risk policy');
    expect(h.filter((x) => x === 'Risk policy')).toHaveLength(1);
    expect(el.textContent).toContain('Reload system settings');
  });

  it('tells an admin how to turn the assistant on while it is off (F42)', async () => {
    assistantOff.set(true);
    await setup(ADMIN);
    await open('System');
    const panel = el.querySelector('#assistant-title')!.closest('section')!;
    expect(panel.textContent).toContain('The assistant is off');
    expect(panel.textContent).toContain('restart');
    assistantOff.set(false);
    fixture.detectChanges();
    expect(el.querySelector('#assistant-title')).toBeNull();
  });

  it('shows the broker, risk policy, data sources and cost presets', async () => {
    await setup();
    await open('System');
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
    await open('System');
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
    (await nextRequest(http, '/api/auth/me')).flush({ ...TRADER, via: 'token' });
    await tick();
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
    (await nextRequest(http, '/api/auth/me')).flush({ ...TRADER, via: 'token' });
    await tick();
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
    (await nextRequest(http, '/api/auth/me')).flush({ ...TRADER, via: 'token' });
    await tick();
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

  it('saving a token calls /api/auth/me with the new bearer and updates the role (UX-37)', async () => {
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    await setup(TRADER);
    typeToken(SECRET);
    button('Save token').click();
    const me = await nextRequest(http, '/api/auth/me');
    expect(me.request.headers.get('Authorization')).toBe(`Bearer ${SECRET}`);
    me.flush({ ...TRADER, role: 'viewer', via: 'token' });
    await tick();
    expect(TestBed.inject(SessionService).role()).toBe('viewer');
    expect(success).toHaveBeenCalledWith(
      'Token saved for this tab. You are Ann, viewer.',
      'Token saved',
    );
  });

  it('drops a saved token the server refuses (UX-37)', async () => {
    const error = vi.spyOn(TestBed.inject(ToastService), 'error');
    await setup(TRADER);
    typeToken('wrong');
    button('Save token').click();
    (await nextRequest(http, '/api/auth/me')).flush(
      { title: 'x', status: 401, detail: 'not_authenticated' },
      { status: 401, statusText: 'Unauthorized' },
    );
    const again = await nextRequest(http, '/api/auth/me');
    expect(again.request.headers.has('Authorization')).toBe(false);
    again.flush(TRADER);
    await tick();
    expect(TestBed.inject(AuthTokenService).token()).toBeNull();
    expect(TestBed.inject(SessionService).me()?.role).toBe('trader');
    expect(error).toHaveBeenCalledWith(
      'That token did not work, so it was not kept. Check it and try again.',
    );
  });

  it('explains that a token cannot be verified when none is saved', async () => {
    await setup(TRADER);
    button('Test token').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Save a token first');
    http.verify();
  });

  it('saves your own risk limits, percents as fractions', async () => {
    await setup(TRADER);
    await open('Risk limits');
    const input = el.querySelector<HTMLInputElement>('#limit-max_weight_per_ticker')!;
    input.value = '20';
    input.dispatchEvent(new Event('input'));
    const positions = el.querySelector<HTMLInputElement>('#limit-max_open_positions')!;
    positions.value = '8';
    positions.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    button('Save limits').click();
    const req = await nextRequest(http, '/api/risk/limits', 'PUT');
    expect(req.request.body).toEqual({
      limits: { max_weight_per_ticker: 0.2, max_open_positions: 8 },
    });
    req.flush(limitsView({ max_weight_per_ticker: 0.2, max_open_positions: 8 }));
    await tick();
    fixture.detectChanges();
    expect(el.querySelector<HTMLInputElement>('#limit-max_weight_per_ticker')!.value).toBe('20');
    http.verify();
  });

  it('refuses a percent over 100 before sending', async () => {
    await setup(TRADER);
    await open('Risk limits');
    const input = el.querySelector<HTMLInputElement>('#limit-cash_buffer_fraction')!;
    input.value = '150';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    button('Save limits').click();
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('Cash to keep is a percent, at most 100.');
    http.expectNone('/api/risk/limits');
    http.verify();
  });
});
