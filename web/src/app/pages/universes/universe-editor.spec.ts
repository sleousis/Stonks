import { Component, signal } from '@angular/core';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';

import type { UniverseView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
import { METRICS } from '../../../testing/screener-fixtures';
import { UniverseEditor } from './universe-editor';

@Component({
  imports: [UniverseEditor],
  template: `
    <app-universe-editor
      [mode]="mode()"
      [universe]="universe()"
      [universes]="universes"
      (saved)="saved.push($event)"
      (cancelled)="cancelled = cancelled + 1"
    />
  `,
})
class Host {
  readonly mode = signal<'create' | 'edit'>('create');
  readonly universe = signal<UniverseView | null>(null);
  universes: UniverseView[] = [
    { id: 'sp500', name: 'S&P 500', kind: 'index', spec: {} },
    { id: 'cheap', name: 'Cheap', kind: 'rule', spec: {} },
  ];
  saved: UniverseView[] = [];
  cancelled = 0;
}

const RULE: UniverseView = {
  id: 'cheap',
  name: 'Cheap',
  kind: 'rule',
  spec: {
    start: '2025-01-02',
    rebalance: 'weekly',
    universe_id: 'sp500',
    filters: [{ metric: 'dividend_yield', min: 0.04, max: null }],
    sort_by: 'dividend_yield',
    descending: true,
    limit: 20,
  },
};

describe('UniverseEditor', () => {
  let fixture: ComponentFixture<Host>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let host: Host;

  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  function type(selector: string, value: string, event = 'input'): void {
    const input = el.querySelector<HTMLInputElement | HTMLSelectElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event(event));
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(Host);
    host = fixture.componentInstance;
    el = fixture.nativeElement;
  });

  afterEach(() => http.verify());

  it('builds a rule from the screener filter builder', async () => {
    fixture.detectChanges();
    type('#u-id', 'payers');
    type('#u-kind', 'rule', 'change');
    (await nextRequest(http, '/api/screener/metrics')).flush(METRICS);
    await settle();
    // The rule can start from another stored universe.
    const from = el.querySelector<HTMLSelectElement>('#u-rule-universe')!;
    expect([...from.options].map((o) => o.value)).toEqual(['', 'sp500', 'cheap']);
    type('#u-rule-universe', 'sp500', 'change');

    button('Add a filter').click();
    fixture.detectChanges();
    type('select[id^="u-f-metric-"]', 'dividend_yield', 'change');
    type('input[id^="u-f-min-"]', '3');
    type('#u-sort', 'dividend_yield', 'change');
    type('#u-limit', '25');

    button('Create universe').click();
    const post = await nextRequest(http, '/api/universes', 'POST');
    expect(post.request.body).toMatchObject({ id: 'payers', kind: 'rule' });
    expect(post.request.body.spec).toEqual({
      rebalance: 'monthly',
      start: '2020-01-01',
      end: null,
      universe_id: 'sp500',
      asset_classes: ['equity'],
      min_price: 5,
      min_adv: 1000000,
      filters: [{ metric: 'dividend_yield', min: 0.03, max: null }],
      sort_by: 'dividend_yield',
      descending: true,
      limit: 25,
    });
    post.flush({ id: 'payers', kind: 'rule', spec: {} });
    await settle();
    expect(host.saved.map((u) => u.id)).toEqual(['payers']);
  });

  it('says what is wrong with a rule before sending it', async () => {
    fixture.detectChanges();
    type('#u-id', 'bad');
    type('#u-kind', 'rule', 'change');
    (await nextRequest(http, '/api/screener/metrics')).flush(METRICS);
    await settle();
    button('Add a filter').click();
    fixture.detectChanges();
    button('Create universe').click();
    fixture.detectChanges();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Pick a metric');
    expect(http.match((r) => r.method === 'POST')).toEqual([]);
  });

  it('imports an index history file before creating the index universe', async () => {
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    fixture.detectChanges();
    type('#u-id', 'toy');
    type('#u-kind', 'index', 'change');
    type('#u-index-id', 'toy');
    const file = new File(['date,ticker,action\n2026-01-02,AAA.US,member\n'], 'toy.csv');
    const input = el.querySelector<HTMLInputElement>('#u-index-file')!;
    Object.defineProperty(input, 'files', { value: [file] });
    input.dispatchEvent(new Event('change'));
    await settle();
    expect(el.textContent).toContain('Chosen: toy.csv.');

    button('Create universe').click();
    const imported = await nextRequest(http, '/api/universes/index-history', 'POST');
    expect(imported.request.body).toEqual({
      index_id: 'toy',
      format: 'csv',
      content: 'date,ticker,action\n2026-01-02,AAA.US,member\n',
    });
    imported.flush({ index_id: 'toy', as_of: '2026-01-02', constituents: 1, changes: 0 });
    const post = await nextRequest(http, '/api/universes', 'POST');
    expect(post.request.body.spec).toMatchObject({ index_id: 'toy' });
    post.flush({ id: 'toy', kind: 'index', spec: {} });
    await settle();
    expect(success).toHaveBeenCalledWith('Imported 1 members and 0 changes for toy.');
    expect(host.saved).toHaveLength(1);
  });

  it('edits a stored rule: the fields come back, the id stays, the save is a PUT', async () => {
    host.mode.set('edit');
    host.universe.set(RULE);
    fixture.detectChanges();
    expect(el.textContent).toContain('Loading the definition');
    (await nextRequest(http, '/api/screener/metrics')).flush(METRICS);
    await settle();

    const id = el.querySelector<HTMLInputElement>('#u-id')!;
    expect(id.value).toBe('cheap');
    expect(id.readOnly).toBe(true);
    expect(el.querySelector<HTMLSelectElement>('#u-rebalance')!.value).toBe('weekly');
    // Percent metrics come back in percent.
    expect(el.querySelector<HTMLInputElement>('input[id^="u-f-min-"]')!.value).toBe('4');
    // A rule does not offer itself as a starting universe.
    const from = el.querySelector<HTMLSelectElement>('#u-rule-universe')!;
    expect([...from.options].map((o) => o.value)).toEqual(['', 'sp500']);

    type('#u-limit', '10');
    button('Save changes').click();
    const put = await nextRequest(http, '/api/universes/cheap', 'PUT');
    expect(put.request.body.id).toBeUndefined();
    expect(put.request.body.spec).toMatchObject({
      start: '2025-01-02',
      rebalance: 'weekly',
      filters: [{ metric: 'dividend_yield', min: 0.04, max: null }],
      limit: 10,
    });
    put.flush({ ...RULE, spec: { ...RULE.spec, limit: 10 } });
    await settle();
    expect(host.saved[0].spec['limit']).toBe(10);
  });

  it('opens a definition the fields cannot hold as JSON, so nothing is lost', async () => {
    host.mode.set('edit');
    host.universe.set({
      id: 'dated',
      kind: 'list',
      spec: { spans: [{ ticker: 'OLD.US', start_date: '2020-01-02' }] },
    });
    fixture.detectChanges();
    await settle();
    const spec = el.querySelector<HTMLTextAreaElement>('#u-spec')!;
    expect(JSON.parse(spec.value)).toEqual({
      spans: [{ ticker: 'OLD.US', start_date: '2020-01-02' }],
    });
    button('Cancel').click();
    expect(host.cancelled).toBe(1);
  });
});
