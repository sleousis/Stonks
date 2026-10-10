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
import { SessionService } from '../../core/auth/session.service';
import { FeatureFlagsService } from '../../core/features/feature-flags.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, tick } from '../../../testing/http';
import { SettingsPage } from './settings.page';
import { provideFakeCalendars } from '../../../testing/fake-calendars';

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
      (await nextRequest(http, '/api/sources')).flush(SOURCES);
      (await nextRequest(http, '/api/lab/cost-models')).flush(COSTS);
      // The System settings form asks only once its tab is open.
      http.expectNone('/api/settings/system');
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
    if (label === 'System') {
      (await nextRequest(http, '/api/settings/system')).flush({ items: [] });
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
    // Sign-in lives on Profile and alert settings on Notifications: no signpost tabs.
    expect(tabs()).toEqual(['Display', 'Risk limits']);
    expect(el.querySelector('[role="tab"][aria-selected="true"]')?.textContent?.trim()).toBe(
      'Display',
    );
    const panel = el.querySelector('[role="tabpanel"]')!;
    expect(panel.getAttribute('aria-labelledby')).toBe('settings-tab-display');
    const h = headings();
    expect(h).toContain('Theme');
    expect(h).toContain('Keyboard');
    expect(h).not.toContain('API token');
    expect(el.querySelector('#api-token')).toBeNull();
    expect(el.querySelector('details.scripts')).toBeNull();
    expect(el.textContent).not.toContain('Reload system settings');
    // No system reads for a trader.
    http.expectNone('/api/brokers');
    http.expectNone('/api/risk/policy');
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
    expect(tabs()).toEqual(['Display', 'Risk limits', 'System']);
    await open('System');
    const h = headings();
    expect(h).toContain('System');
    expect(h).toContain('Broker');
    // The editable System settings hold the risk limits: no read-only copy.
    expect(h).not.toContain('Risk policy');
    http.expectNone('/api/risk/policy');
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

  it('shows the broker, data sources and cost presets', async () => {
    await setup();
    await open('System');
    const text = el.textContent ?? '';
    expect(text).not.toContain('[production');
    expect(text).toContain('Simulated');
    expect(text).toContain('Paper');
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
