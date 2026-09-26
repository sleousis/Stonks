import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { BrokerInfo, GoLiveReport, Page, RiskPolicy, StrategySummary } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { tick } from '../../../testing/http';
import { STRATEGY_METADATA } from '../../../testing/strategy-fixtures';
import { GoLivePage, checkRow } from './go-live.page';

function strategy(id: string, status: StrategySummary['status']): StrategySummary {
  return {
    id,
    status,
    class_path: 'stonks.strategies.momentum.MomentumStrategy',
    applicable_asset_classes: ['equity'],
    params: {},
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
    metadata: STRATEGY_METADATA,
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

function report(id: string, passed: boolean): GoLiveReport {
  return {
    strategy_id: id,
    status: 'shadow',
    source: 'shadow',
    passed,
    policy: { min_days: 20, max_drawdown: 0.15, max_drift: 0.1, min_trades: 5 },
    checks: [
      {
        name: 'status',
        passed: true,
        value: null,
        limit: null,
        detail: 'shadow: paper period from shadow P&L',
      },
      {
        name: 'min_days',
        passed,
        value: passed ? 25 : 3,
        limit: 20,
        detail: passed ? '25 paper day(s), need >= 20' : '3 paper day(s), need >= 20',
      },
      {
        name: 'max_drawdown',
        passed: true,
        value: 0.042,
        limit: 0.15,
        detail: 'max drawdown 4.20%, limit 15.00%',
      },
      {
        name: 'max_drift',
        passed: true,
        value: -0.013,
        limit: 0.1,
        detail: 'paper +1.00% vs backtest +2.30% (gap -1.30%, limit ±10.00%)',
      },
      { name: 'min_trades', passed: true, value: 7, limit: 5, detail: '7 filled trade(s)' },
      { name: 'survival', passed: true, value: 4, limit: 4, detail: '4/4 passed' },
    ],
  };
}

describe('GoLivePage', () => {
  let fixture: ComponentFixture<GoLivePage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let goliveRequests: string[];
  let goliveError: boolean;

  beforeEach(() => {
    goliveRequests = [];
    goliveError = false;
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
        else if (path.endsWith('/golive')) {
          const id = decodeURIComponent(path.split('/')[3]);
          if (goliveError) {
            req.flush(
              { title: 'Not found', status: 404, detail: 'no strategy' },
              { status: 404, statusText: 'Not Found' },
            );
          } else {
            goliveRequests.push(id);
            req.flush(report(id, id === 'momentum-v3'));
          }
        } else throw new Error(`unexpected request ${path}`);
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

  it('shows each check with its value, limit and the overall verdict', async () => {
    fixture.componentRef.setInput('strategy', 'buyhold-spy');
    fixture.detectChanges();
    await flushAll();

    expect(goliveRequests).toEqual(['buyhold-spy']);
    const check = el.querySelector('section[aria-labelledby="check-title"]')!;
    expect(check.querySelector('.verdict')?.textContent).toContain('Not ready');
    expect(check.textContent).toContain('1 of 6 checks failed');
    const rows = Array.from(check.querySelectorAll('.checks li'));
    expect(rows.length).toBe(6);
    const minDays = rows[1];
    expect(minDays.textContent).toContain('min_days');
    expect(minDays.querySelector('app-status-pill')?.textContent).toContain('fail');
    expect(minDays.querySelector('.check-value')?.textContent).toContain('3');
    expect(minDays.querySelector('.check-limit')?.textContent).toContain('20');
    expect(rows[2].querySelector('.check-value')?.textContent).toContain('4.20%');
    expect(rows[3].querySelector('.check-value')?.textContent).toContain('-1.30%');
    expect(checkRow({ ...report('x', true).checks[2], value: -0 }).value).toBe('0.00%');
    expect(rows[5].querySelector('.check-value')?.textContent).toContain('4 of 4');
    expect(check.querySelector('app-cli-command code')?.textContent).toContain(
      'stonks golive check buyhold-spy',
    );
    expect(el.querySelector<HTMLSelectElement>('select')?.value).toBe('buyhold-spy');
    expect(el.querySelector('a[href="/strategies/buyhold-spy"]')).not.toBeNull();
  });

  it('switches strategy from the picker and loads its check', async () => {
    fixture.detectChanges();
    await flushAll();

    const select = el.querySelector<HTMLSelectElement>('select')!;
    select.value = 'momentum-v3';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    await flushAll();

    expect(goliveRequests).toEqual(['momentum-v3']);
    const check = el.querySelector('section[aria-labelledby="check-title"]')!;
    expect(check.querySelector('.verdict')?.textContent).toContain('Ready for promotion');
    expect(check.querySelector('app-cli-command code')?.textContent).toContain(
      'stonks golive check momentum-v3',
    );
  });

  it('shows an error when the check cannot load', async () => {
    goliveError = true;
    fixture.componentRef.setInput('strategy', 'buyhold-spy');
    fixture.detectChanges();
    await flushAll();
    expect(el.textContent).toContain('Could not run the go-live check');
  });

  it('flags an id that is not registered', async () => {
    fixture.componentRef.setInput('strategy', 'ghost');
    fixture.detectChanges();
    await flushAll();
    expect(el.textContent).toContain('Strategy not found');
    expect(el.querySelector('app-cli-command')).toBeNull();
    expect(goliveRequests).toEqual([]);
  });
});
