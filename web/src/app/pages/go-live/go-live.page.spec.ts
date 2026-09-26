import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { BrokerInfo, Page, RiskPolicy, StrategySummary } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { tick } from '../../../testing/http';
import { GATE_CHECKS, GoLivePage } from './go-live.page';

function strategy(id: string, status: StrategySummary['status']): StrategySummary {
  return {
    id,
    status,
    class_path: 'stonks.strategies.momentum.MomentumStrategy',
    applicable_asset_classes: ['equity'],
    params: {},
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
  };
}

const STRATEGIES: Page<StrategySummary> = {
  items: [strategy('momentum-v3', 'active'), strategy('buyhold-spy', 'shadow')],
  total: 2,
  limit: 500,
  offset: 0,
};
const BROKER: BrokerInfo = {
  kind: 'alpaca',
  paper: true,
  allow_live: false,
  credentials_configured: true,
};
const RISK: RiskPolicy = {
  enabled: true,
  max_weight_per_ticker: 0.2,
  max_open_positions: 10,
  cash_buffer_fraction: 0.05,
  min_order_notional: 100,
  max_weight_per_asset_class: { crypto: 0.1 },
};

describe('GoLivePage', () => {
  let fixture: ComponentFixture<GoLivePage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [GoLivePage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(GoLivePage);
    el = fixture.nativeElement;
  });

  afterEach(() => controller.verify());

  async function flushAll(): Promise<void> {
    for (let i = 0; i < 5; i++) {
      for (const req of controller.match(() => true)) {
        const path = req.request.url.split('?')[0];
        if (path === '/api/strategies') req.flush(STRATEGIES);
        else if (path === '/api/brokers') req.flush(BROKER);
        else if (path === '/api/risk/policy') req.flush(RISK);
        else throw new Error(`unexpected request ${path}`);
      }
      await tick(5);
      fixture.detectChanges();
    }
  }

  it('asks for a strategy, shadow ones first, and shows broker and risk', async () => {
    fixture.detectChanges();
    await flushAll();

    expect(el.querySelector('h1')?.textContent).toContain('Go-live');
    expect(el.textContent).toContain('Pick a strategy');
    const options = Array.from(el.querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['', 'buyhold-spy', 'momentum-v3']);

    const broker = el.querySelector('section[aria-labelledby="broker-title"]');
    expect(broker?.textContent).toContain('Alpaca');
    expect(broker?.textContent).toContain('Paper');
    expect(broker?.textContent).toContain('Not allowed');

    const risk = el.querySelector('section[aria-labelledby="risk-title"]');
    expect(risk?.textContent).toContain('20.0%');
    expect(risk?.textContent).toContain('Max weight, crypto');
  });

  it('says the check is not available yet and gives the CLI command', async () => {
    fixture.componentRef.setInput('strategy', 'buyhold-spy');
    fixture.detectChanges();
    await flushAll();

    const check = el.querySelector('section[aria-labelledby="check-title"]');
    expect(check?.textContent).toContain('Not available yet');
    expect(check?.querySelector('app-cli-command code')?.textContent).toContain(
      'stonks golive check buyhold-spy',
    );
    expect(check?.querySelectorAll('.checks li').length).toBe(GATE_CHECKS.length);
    expect(el.querySelector<HTMLSelectElement>('select')?.value).toBe('buyhold-spy');
    expect(el.querySelector('a[href="/strategies/buyhold-spy"]')).not.toBeNull();
  });

  it('switches strategy from the picker', async () => {
    fixture.detectChanges();
    await flushAll();

    const select = el.querySelector<HTMLSelectElement>('select')!;
    select.value = 'momentum-v3';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    expect(el.querySelector('app-cli-command code')?.textContent).toContain(
      'stonks golive check momentum-v3',
    );
  });

  it('flags an id that is not registered', async () => {
    fixture.componentRef.setInput('strategy', 'ghost');
    fixture.detectChanges();
    await flushAll();
    expect(el.textContent).toContain('Strategy not found');
    expect(el.querySelector('app-cli-command')).toBeNull();
  });
});
