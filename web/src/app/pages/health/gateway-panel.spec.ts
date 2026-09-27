import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { GatewayHealthView, GatewayView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { GatewayPanel, faultWords, gatewayState, whenText } from './gateway-panel';

function gateway(over: Partial<GatewayView> = {}): GatewayView {
  return {
    gateway: 'live',
    mode: 'live',
    connected: true,
    checked: true,
    last_check_at: '2026-09-27T14:00:00Z',
    last_ok_at: '2026-09-27T14:00:00Z',
    down_since: null,
    consecutive_failures: 0,
    fault: null,
    detail: 'port open',
    latency_ms: 3,
    paused_at: null,
    your_portfolios: ['Main live'],
    paused_books: [],
    paused_elsewhere: 0,
    ...over,
  };
}

const DOWN = gateway({
  connected: false,
  last_check_at: '2026-09-27T14:00:00Z',
  last_ok_at: '2026-09-25T14:00:00Z',
  down_since: '2026-09-25T14:05:00Z',
  consecutive_failures: 12,
  fault: 'login_refused',
  detail: 'ConnectionRefusedError',
  paused_at: '2026-09-27T14:00:00Z',
  paused_books: [
    {
      subscription_id: 'sub_1',
      portfolio_id: 'pf_live',
      portfolio_name: 'Main live',
      strategy_id: 'momentum_3fa9c21b',
      reason: 'broker error: gateway live login_refused',
    },
  ],
  paused_elsewhere: 2,
});

describe('gateway words', () => {
  it('tells connected, down and not checked apart', () => {
    expect(gatewayState(gateway()).label).toBe('Connected');
    expect(gatewayState(DOWN)).toEqual({ status: 'unhealthy', label: 'Down', tone: 'negative' });
    expect(gatewayState(gateway({ checked: false, connected: false })).label).toBe(
      'Not checked yet',
    );
  });

  it('puts faults and times in plain words', () => {
    expect(faultWords('login_refused')).toBe('The broker refused the login');
    expect(faultWords('odd_thing')).toBe('odd thing');
    expect(faultWords(null)).toBeNull();
    expect(whenText(null)).toBe('Never');
  });
});

describe('GatewayPanel', () => {
  let fixture: ComponentFixture<GatewayPanel>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(view: GatewayHealthView): Promise<HTMLElement> {
    fixture = TestBed.createComponent(GatewayPanel);
    fixture.detectChanges();
    (await nextRequest(http, '/api/brokers/gateways')).flush(view);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('says when no gateway is set up', async () => {
    const el = await render({ configured: false, gateways: [] });
    expect(el.textContent).toContain('No broker gateway');
  });

  it('shows a down gateway with its fault and the auto strategies it paused', async () => {
    const el = await render({
      configured: true,
      gateways: [DOWN, gateway({ gateway: 'paper', mode: 'paper', your_portfolios: [] })],
    });
    const cards = [...el.querySelectorAll('.gateway')];
    expect(cards.length).toBe(2);
    expect(el.querySelector('.down')?.textContent).toContain('1 down');

    const live = cards[0];
    expect(live.getAttribute('data-state')).toBe('unhealthy');
    expect(live.textContent).toContain('LIVE');
    expect(live.textContent).toContain('Down');
    expect(live.textContent).toContain('Last good check');
    expect(live.textContent).toContain('12');
    expect(live.querySelector('.problem')?.textContent).toContain('The broker refused the login');
    const link = live.querySelector<HTMLAnchorElement>('.paused a')!;
    expect(link.textContent).toContain('Momentum 3fa9');
    expect(link.getAttribute('href')).toBe('/strategies/momentum_3fa9c21b');
    expect(live.textContent).toContain('2 more in other people');

    const paper = cards[1];
    expect(paper.textContent).toContain('PAPER');
    expect(paper.textContent).toContain('Connected');
    expect(paper.querySelector('.problem')).toBeNull();
    expect(paper.querySelector('.paused')).toBeNull();
  });
});
