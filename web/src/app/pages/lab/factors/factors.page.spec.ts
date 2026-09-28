import {
  HttpTestingController,
  type TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { tick } from '../../../../testing/http';
import type { FactorCatalogView } from '../../../api/models';
import { provideApi } from '../../../api/provide-api';
import { FACTOR_CATALOG, PIOTROSKI } from './factor-test-fixtures';
import { FactorsPage } from './factors.page';

describe('FactorsPage', () => {
  let fixture: ComponentFixture<FactorsPage>;
  let controller: HttpTestingController;
  let el: HTMLElement;
  let queries: Record<string, string>[];
  let catalog: FactorCatalogView;
  let status: number;

  beforeEach(async () => {
    queries = [];
    catalog = FACTOR_CATALOG;
    status = 200;
    TestBed.configureTestingModule({
      imports: [FactorsPage],
      providers: [...provideApi(), provideHttpClientTesting(), provideRouter([])],
    });
    controller = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(FactorsPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  });

  afterEach(() => controller.verify());

  function respond(req: TestRequest): void {
    const url = new URL(req.request.urlWithParams, 'http://localhost');
    if (url.pathname !== '/api/factors') throw new Error(`unexpected ${url.pathname}`);
    queries.push(Object.fromEntries(url.searchParams));
    if (status !== 200)
      return req.flush({ title: 'x', status, detail: 'Down.' }, { status, statusText: 'x' });
    req.flush(catalog);
  }

  async function settle(rounds = 6): Promise<void> {
    for (let i = 0; i < rounds; i++) {
      controller.match(() => true).forEach(respond);
      await tick(5);
      fixture.detectChanges();
    }
  }

  function rows(): string[] {
    return [...el.querySelectorAll('app-data-table tbody tr')].map((r) => r.textContent!);
  }

  it('lists every factor with a link, its set, family and direction', () => {
    expect(queries[0]).toEqual({});
    const r = rows();
    expect(r.length).toBe(3);
    expect(r.join()).toContain('Lower'); // low_vol_60
    const link = el.querySelector<HTMLAnchorElement>('a[href="/lab/factors/mom_12_1"]');
    expect(link?.textContent).toContain('mom_12_1');
    expect(el.querySelector('.count')!.textContent).toContain('3 of 3');
    expect(el.querySelector('a[href="/lab/factors/formula"]')?.textContent).toContain(
      'Write a formula',
    );
  });

  it('filters by typed text on the page', () => {
    const search = el.querySelector<HTMLInputElement>('#factor-search')!;
    search.value = 'piotroski';
    search.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(rows().length).toBe(1);
    expect(rows()[0]).toContain(PIOTROSKI.id);
    search.value = 'nothing like it';
    search.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.textContent).toContain('No factor matches');
  });

  it('asks the server for one set, family or kind', async () => {
    fixture.componentRef.setInput('set', 'fundamentals');
    fixture.componentRef.setInput('kind', 'fundamental');
    fixture.componentRef.setInput('family', 'quality');
    catalog = { ...FACTOR_CATALOG, factors: [PIOTROSKI] };
    fixture.detectChanges();
    await settle();
    expect(queries.at(-1)).toEqual({ set: 'fundamentals', family: 'quality', kind: 'fundamental' });
    expect(rows().length).toBe(1);
    const setSelect = el.querySelector<HTMLSelectElement>('#factor-set')!;
    expect(setSelect.value).toBe('fundamentals');
  });

  it('shows an error with retry when the library fails to load', async () => {
    status = 500;
    fixture.componentRef.setInput('set', 'classic');
    fixture.detectChanges();
    await settle();
    expect(el.textContent).toContain('Could not load the factor library');
    status = 200;
    [...el.querySelectorAll<HTMLButtonElement>('button')]
      .find((b) => b.textContent!.includes('Try again'))!
      .click();
    await settle();
    expect(rows().length).toBe(3);
  });
});
