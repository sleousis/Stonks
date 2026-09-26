import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { provideApi } from '../../../api/provide-api';
import { CommandRegistry } from '../../../core/commands/command-registry';
import { ShortcutsService } from '../../../core/commands/shortcuts.service';
import { ToastService } from '../../../core/notify/toast.service';
import { nextRequest, tick } from '../../../../testing/http';
import { STRATEGY_METADATA } from '../../../../testing/strategy-fixtures';
import { CommandPalette, jobLabel, jobPath } from './command-palette';

const STRATEGIES = {
  items: [
    {
      id: 'momentum-v3',
      status: 'active',
      class_path: 'stonks.strategies.momentum.MomentumStrategy',
      applicable_asset_classes: ['equity'],
      params: {},
      created_at: '2026-09-01T10:00:00Z',
      updated_at: '2026-09-20T10:00:00Z',
      metadata: STRATEGY_METADATA,
    },
    {
      id: 'buy-hold',
      status: 'shadow',
      class_path: 'stonks.strategies.buy_and_hold.BuyAndHold',
      applicable_asset_classes: ['equity'],
      params: {},
      created_at: '2026-09-01T10:00:00Z',
      updated_at: '2026-09-20T10:00:00Z',
      metadata: STRATEGY_METADATA,
    },
  ],
  total: 2,
  limit: 200,
  offset: 0,
};

const JOBS = {
  items: [
    {
      id: 'job-1',
      kind: 'backtest',
      status: 'succeeded',
      progress: 1,
      params: { strategy: { strategy_id: 'momentum-v3' } },
      created_at: new Date(Date.now() - 120_000).toISOString(),
    },
  ],
  total: 1,
  limit: 8,
  offset: 0,
};

describe('CommandPalette', () => {
  let fixture: ComponentFixture<CommandPalette>;
  let controller: HttpTestingController;
  let shortcuts: ShortcutsService;
  let el: HTMLElement;
  const runLab = vi.fn();

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    shortcuts = TestBed.inject(ShortcutsService);
    TestBed.inject(CommandRegistry).register([
      { id: 'page.lab', label: 'Lab', group: 'Pages', hint: 'g l', run: runLab },
      { id: 'page.orders', label: 'Orders', group: 'Pages', run: vi.fn() },
      {
        id: 'action.tick',
        label: 'Run a dry-run tick',
        group: 'Actions',
        keywords: ['simulate'],
        run: vi.fn(),
      },
    ]);
    runLab.mockReset();
    fixture = TestBed.createComponent(CommandPalette);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  async function openAndLoad() {
    shortcuts.openPalette();
    fixture.detectChanges();
    (await nextRequest(controller, '/api/strategies')).flush(STRATEGIES);
    (await nextRequest(controller, '/api/jobs')).flush(JOBS);
    await tick();
    fixture.detectChanges();
  }

  const input = () => el.querySelector<HTMLInputElement>('input[role="combobox"]')!;
  const options = () => [...el.querySelectorAll<HTMLElement>('[role="option"]')];
  const labels = () => options().map((o) => o.querySelector('.option-label')!.textContent!.trim());
  const groups = () =>
    [...el.querySelectorAll('[role="group"] .section-title')].map((g) => g.textContent!.trim());

  async function type(text: string) {
    input().value = text;
    input().dispatchEvent(new Event('input'));
    fixture.detectChanges();
    await tick(200);
    fixture.detectChanges();
  }

  function press(key: string) {
    input().dispatchEvent(new KeyboardEvent('keydown', { key, cancelable: true }));
    fixture.detectChanges();
  }

  it('follows the ARIA combobox pattern', async () => {
    await openAndLoad();
    const combo = input();
    expect(el.querySelector('dialog')!.hasAttribute('open')).toBe(true);
    expect(document.activeElement).toBe(combo);
    expect(combo.getAttribute('aria-controls')).toBe('palette-listbox');
    expect(combo.getAttribute('aria-expanded')).toBe('true');
    expect(el.querySelector('#palette-listbox')!.getAttribute('role')).toBe('listbox');
    const first = options()[0];
    expect(combo.getAttribute('aria-activedescendant')).toBe(first.id);
    expect(first.getAttribute('aria-selected')).toBe('true');
  });

  it('shows actions, recent jobs and pages before typing', async () => {
    await openAndLoad();
    expect(groups()).toEqual(['Actions', 'Recent jobs', 'Pages']);
    expect(labels()).toContain('Backtest: momentum-v3');
  });

  it('searches pages, strategies and tickers as you type', async () => {
    await openAndLoad();
    await type('mom');
    (await nextRequest(controller, '/api/market/instruments')).flush({
      items: [{ id: 'MOMO.US', name: 'Hello Group', exchange: 'US' }],
      total: 1,
      limit: 8,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    expect(groups()).toEqual(['Strategies', 'Tickers', 'Recent jobs']);
    expect(labels()).toEqual(['momentum-v3', 'MOMO.US', 'Backtest: momentum-v3']);
    expect(el.querySelector('[role="status"]')!.textContent).toContain('3 results');
  });

  it('asks the API for tickers once typing pauses', async () => {
    await openAndLoad();
    input().value = 'a';
    input().dispatchEvent(new Event('input'));
    input().value = 'aa';
    input().dispatchEvent(new Event('input'));
    fixture.detectChanges();
    await tick(200);
    fixture.detectChanges();
    const req = await nextRequest(controller, '/api/market/instruments');
    expect(new URL(req.request.urlWithParams, 'http://x').searchParams.get('q')).toBe('aa');
    expect(req.request.headers.has('X-Stonks-Silent')).toBe(false); // stripped by the interceptor
    req.flush({ items: [], total: 0, limit: 8, offset: 0 });
  });

  it('moves with the arrow keys and runs the active option on Enter', async () => {
    await openAndLoad();
    await type('lab');
    (await nextRequest(controller, '/api/market/instruments')).flush({
      items: [],
      total: 0,
      limit: 8,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    expect(labels()[0]).toBe('Lab');
    press('ArrowUp');
    expect(input().getAttribute('aria-activedescendant')).toBe(options().at(-1)!.id);
    press('ArrowDown');
    expect(input().getAttribute('aria-activedescendant')).toBe(options()[0].id);
    press('Enter');
    await tick();
    expect(runLab).toHaveBeenCalledOnce();
    expect(shortcuts.paletteOpen()).toBe(false);
  });

  it('opens a strategy on click', async () => {
    await openAndLoad();
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    await type('buy');
    (await nextRequest(controller, '/api/market/instruments')).flush({
      items: [],
      total: 0,
      limit: 8,
      offset: 0,
    });
    await tick();
    fixture.detectChanges();
    options()
      .find((o) => o.textContent?.includes('buy-hold'))!
      .click();
    await tick();
    expect(navigate).toHaveBeenCalledWith(['/strategies', 'buy-hold']);
  });

  it('says so, without a toast, when a search fails', async () => {
    const toasts = TestBed.inject(ToastService);
    shortcuts.openPalette();
    fixture.detectChanges();
    (await nextRequest(controller, '/api/strategies')).flush(
      { detail: 'down' },
      { status: 503, statusText: 'Unavailable' },
    );
    (await nextRequest(controller, '/api/jobs')).flush(JOBS);
    await tick();
    fixture.detectChanges();
    expect(el.querySelector('.status')!.textContent).toContain('Could not load strategies');
    expect(toasts.toasts().length).toBe(0);
  });

  it('closes on the Close button', async () => {
    await openAndLoad();
    [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Close'))!.click();
    fixture.detectChanges();
    expect(shortcuts.paletteOpen()).toBe(false);
    expect(el.querySelector('dialog')!.hasAttribute('open')).toBe(false);
  });
});

describe('job routing', () => {
  it('sends each job kind to the page that shows it', () => {
    expect(jobPath('tick')).toBe('/orders/ticks');
    expect(jobPath('backtest')).toBe('/lab');
    expect(jobPath('lab_run')).toBe('/lab');
    expect(jobPath('ingest')).toBe('/data');
    expect(jobPath('other')).toBe('/');
    expect(jobLabel('lab_run')).toBe('Lab run');
    expect(jobLabel('data_sync')).toBe('Data sync');
  });
});
