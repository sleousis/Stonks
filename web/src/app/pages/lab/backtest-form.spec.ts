import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import { TRADER } from '../../../testing/auth-fixtures';
import { nextRequest } from '../../../testing/http';
import { CATALOG, MOMENTUM } from '../../../testing/lab-fixtures';
import type { BacktestRequest, MeView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { BacktestFormView } from './backtest-form';

describe('BacktestFormView', () => {
  let fixture: ComponentFixture<BacktestFormView>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let emitted: BacktestRequest[];

  beforeEach(() => {
    emitted = [];
    TestBed.configureTestingModule({
      imports: [BacktestFormView],
      providers: [...provideApi(), provideHttpClientTesting()],
    });
    controller = TestBed.inject(HttpTestingController);
  });

  afterEach(() => controller.verify());

  async function create(me: MeView = TRADER): Promise<void> {
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    fixture = TestBed.createComponent(BacktestFormView);
    fixture.componentRef.setInput('classes', CATALOG);
    fixture.componentRef.setInput('costModels', [
      { name: 'realistic', description: 'Retail fees and spread.', settings: {} },
    ]);
    fixture.componentInstance.submitted.subscribe((r) => emitted.push(r));
    el = fixture.nativeElement;
    fixture.detectChanges();
  }

  function type(selector: string, value: string): void {
    const node = el.querySelector<HTMLInputElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function choose(selector: string, value: string): void {
    const node = el.querySelector<HTMLSelectElement>(selector)!;
    node.value = value;
    node.dispatchEvent(new Event('change'));
    fixture.detectChanges();
  }

  function submit(): void {
    el.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
  }

  it('starts from the tickers of a watchlist opened in the lab', async () => {
    await create();
    fixture.componentRef.setInput('tickers', 'AAPL.US, MSFT.US');
    fixture.detectChanges();
    expect(el.querySelector<HTMLTextAreaElement>('#bt-tickers')!.value).toBe('AAPL.US, MSFT.US');
  });

  it('emits the request with parameters, tickers and flat costs', async () => {
    await create();
    el.querySelector<HTMLInputElement>(`input[value="${MOMENTUM.class_path}"]`)!.click();
    fixture.detectChanges();
    type('#bt-tickers', 'aapl.us');
    type('#bt-param-lookback_days', '40');
    choose('#bt-cost', 'flat');
    type('#bt-slippage', '3');
    submit();
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toMatchObject({
      strategy: { class_path: MOMENTUM.class_path, params: { lookback_days: 40 } },
      universe: ['AAPL.US'],
      slippage_bps: 3,
      fee_per_trade: 0,
    });
    expect(emitted[0].cost_model).toBeUndefined();
  });

  it('blocks an incomplete form and says what to fix', async () => {
    await create();
    submit();
    expect(emitted).toEqual([]);
    expect(el.textContent).toContain('Pick a strategy.');
    expect(el.textContent).toContain('Fix the 2 highlighted fields to run it.');
  });

  it('explains the default costs without server setting names', async () => {
    await create();
    const hint = el.querySelector('#bt-cost-hint')!.textContent!;
    expect(hint).toContain('your admin set up');
    expect(hint).not.toMatch(/\[[a-z_.]+\]/);
    choose('#bt-cost', 'realistic');
    expect(el.querySelector('#bt-cost-hint')!.textContent).toContain('Retail fees and spread.');
  });

  it('shows a note instead of Run to someone without lab access', async () => {
    await create({ ...TRADER, role: 'viewer', scopes: ['read'] });
    const run = el.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    expect(run.disabled).toBe(true);
    expect(el.querySelector('.permission-note')?.textContent).toContain('Traders and admins only.');
  });
});
