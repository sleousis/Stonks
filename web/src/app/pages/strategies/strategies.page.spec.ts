import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { Page, StrategySummary } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { nextRequest, tick } from '../../../testing/http';
import { STRATEGY_METADATA } from '../../../testing/strategy-fixtures';
import { StrategiesPage } from './strategies.page';

function strategy(id: string, status: StrategySummary['status'], cls: string): StrategySummary {
  return {
    id,
    status,
    class_path: `stonks.strategies.${cls.toLowerCase()}.${cls}`,
    applicable_asset_classes: ['equity'],
    params: {},
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
    metadata: STRATEGY_METADATA,
  };
}

const ALL: StrategySummary[] = [
  strategy('momentum-v3', 'active', 'MomentumStrategy'),
  strategy('buyhold-spy', 'shadow', 'BuyAndHoldStrategy'),
  strategy('momentum-v1', 'retired', 'MomentumStrategy'),
];

function statusParam(req: TestRequest): string | null {
  return new URL(req.request.urlWithParams, 'http://localhost').searchParams.get('status');
}

function page(items: StrategySummary[]): Page<StrategySummary> {
  return { items, total: items.length, limit: 500, offset: 0 };
}

describe('StrategiesPage', () => {
  let fixture: ComponentFixture<StrategiesPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [StrategiesPage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(StrategiesPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => controller.verify());

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await tick(5);
      fixture.detectChanges();
    }
  }

  function rowIds(): string[] {
    return Array.from(el.querySelectorAll('tbody tr td:first-child')).map(
      (td) => td.textContent?.trim() ?? '',
    );
  }

  it('lists strategies with links to their detail pages', async () => {
    const req = await nextRequest(controller, '/api/strategies');
    expect(statusParam(req)).toBeNull();
    req.flush(page(ALL));
    await settle();

    expect(el.querySelector('h1')?.textContent).toContain('Strategies');
    expect(rowIds().sort()).toEqual(['buyhold-spy', 'momentum-v1', 'momentum-v3']);
    const link = el.querySelector('a[href="/strategies/momentum-v3"]');
    expect(link?.textContent).toContain('momentum-v3');
    expect(el.textContent).toContain('3 strategies');
    expect(el.textContent).toContain('MomentumStrategy');
  });

  it('refetches with the chosen status filter', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();

    const shadow = Array.from(el.querySelectorAll<HTMLLabelElement>('.segment')).find(
      (l) => l.textContent?.trim() === 'Shadow',
    );
    shadow?.querySelector('input')?.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    const req = await nextRequest(controller, '/api/strategies');
    expect(statusParam(req)).toBe('shadow');
    req.flush(page([ALL[1]]));
    await settle();

    expect(rowIds()).toEqual(['buyhold-spy']);
    expect(shadow?.classList).toContain('selected');
  });

  it('filters by search text in the browser and explains no matches', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(page(ALL));
    await settle();

    const input = el.querySelector<HTMLInputElement>('#strategy-search')!;
    input.value = 'momentum';
    input.dispatchEvent(new Event('input'));
    await settle();
    expect(rowIds().sort()).toEqual(['momentum-v1', 'momentum-v3']);
    expect(el.textContent).toContain('2 of 3 strategies');

    input.value = 'zzz';
    input.dispatchEvent(new Event('input'));
    await settle();
    expect(el.textContent).toContain('No matches');

    const clear = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Clear search'),
    );
    clear?.click();
    await settle();
    expect(rowIds().length).toBe(3);
  });

  it('explains an empty status', async () => {
    fixture.componentRef.setInput('status', 'retired');
    fixture.detectChanges();
    // The initial unfiltered request may already be out; answer every one.
    await tick(5);
    for (const req of controller.match(() => true)) req.flush(page([]));
    await settle();
    expect(el.textContent).toContain('No retired strategies');
    expect(el.textContent).toContain('Show all statuses');
  });

  it('shows the API message when the list fails', async () => {
    (await nextRequest(controller, '/api/strategies')).flush(
      { title: 'Service Unavailable', status: 503, detail: 'state store is locked' },
      { status: 503, statusText: 'Service Unavailable' },
    );
    await settle();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('state store is locked');
  });
});
