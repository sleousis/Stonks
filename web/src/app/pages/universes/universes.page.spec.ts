import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { MeView, UniverseView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { UniversesPage } from './universes.page';

const UNIVERSES: UniverseView[] = [
  {
    id: 'us-big',
    name: 'US large caps',
    kind: 'rule',
    spec: {},
    member_count: 480,
    refreshed_at: '2026-09-25T06:00:00Z',
  },
  { id: 'watch', name: null, kind: 'list', spec: {}, member_count: null, refreshed_at: null },
];

describe('UniversesPage', () => {
  let fixture: ComponentFixture<UniversesPage>;
  let http: HttpTestingController;
  let el: HTMLElement;
  let confirm: ReturnType<typeof vi.fn>;
  /** Who is signed in; a describe block can change it in beforeAll. */
  let me: MeView = ADMIN;

  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function button(text: string): HTMLButtonElement | undefined {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  }

  function type(selector: string, value: string, event = 'input'): void {
    const input = el.querySelector<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event(event));
    fixture.detectChanges();
  }

  beforeEach(async () => {
    confirm = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const signingIn = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(me);
    await signingIn;
    fixture = TestBed.createComponent(UniversesPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/universes')).flush(page(UNIVERSES));
    await settle();
  });

  afterEach(() => http.verify());

  it('lists universes with kind, members and last refresh', () => {
    const list = el.querySelector('[aria-labelledby="list-title"]')!;
    expect(list.textContent).toContain('2 stored');
    expect(list.textContent).toContain('Rule');
    expect(list.textContent).toContain('480');
    expect(list.textContent).toContain('Never');
    const link = list.querySelector<HTMLAnchorElement>('a[href="/universes/us-big"]');
    expect(link).not.toBeNull();
  });

  it('creates a universe from its fields and opens it', async () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    button('New universe')!.click();
    fixture.detectChanges();
    button('Create universe')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter an id.');

    type('#u-id', 'tech');
    const kind = el.querySelector<HTMLSelectElement>('#u-kind')!;
    kind.value = 'exchange';
    kind.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    // Each kind asks for its own fields; JSON is an advanced option.
    expect(el.querySelector('#u-spec')).toBeNull();
    expect(el.querySelector<HTMLInputElement>('#u-exchange')!.value).toBe('US');
    expect(el.textContent).not.toContain('Spec (JSON)');
    // CSV is for lists only.
    expect(el.querySelector('input[name="u-source"]')).toBeNull();
    type('#u-exchange', 'lse');
    el.querySelector<HTMLInputElement>('#u-delisted')!.click();
    fixture.detectChanges();

    button('Create universe')!.click();
    const post = await nextRequest(http, '/api/universes', 'POST');
    expect(post.request.body).toMatchObject({ id: 'tech', kind: 'exchange', csv: null });
    expect(post.request.body.spec).toMatchObject({ exchange: 'LSE', include_delisted: false });
    post.flush({ id: 'tech', kind: 'exchange', spec: {} });
    (await nextRequest(http, '/api/universes')).flush(page(UNIVERSES));
    await settle();
    expect(navigate).toHaveBeenCalledWith(['/universes', 'tech']);
  });

  it('asks for at least one ticker on a list', async () => {
    button('New universe')!.click();
    fixture.detectChanges();
    type('#u-id', 'mine');
    type('#u-tickers', ' ');
    button('Create universe')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter at least one ticker.');
    expect(http.match((r) => r.method === 'POST')).toEqual([]);
  });

  it('offers the definition as JSON, as an advanced option', async () => {
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    button('New universe')!.click();
    fixture.detectChanges();
    type('#u-id', 'raw');
    type('#u-tickers', 'nvda.us');
    button('Edit as JSON')!.click();
    fixture.detectChanges();
    const spec = el.querySelector<HTMLTextAreaElement>('#u-spec')!;
    expect(JSON.parse(spec.value)).toMatchObject({ tickers: ['NVDA.US'] });
    expect(el.querySelector('label[for="u-spec"]')!.textContent).toContain('Definition (JSON)');
    type('#u-spec', '{"tickers": ["TSLA.US"]}');
    button('Create universe')!.click();
    const post = await nextRequest(http, '/api/universes', 'POST');
    expect(post.request.body.spec).toEqual({ tickers: ['TSLA.US'] });
    post.flush({ id: 'raw', kind: 'list', spec: {} });
    (await nextRequest(http, '/api/universes')).flush(page(UNIVERSES));
    await settle();
  });

  it('creates a list universe from an uploaded CSV', async () => {
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    button('New universe')!.click();
    fixture.detectChanges();
    type('#u-id', 'watch2');
    el.querySelectorAll<HTMLInputElement>('input[name="u-source"]')[1].click();
    fixture.detectChanges();

    const file = new File(['ticker,start_date\nAAPL.US,2020-01-02\n'], 'names.csv', {
      type: 'text/csv',
    });
    const input = el.querySelector<HTMLInputElement>('#u-csv')!;
    Object.defineProperty(input, 'files', { value: [file] });
    input.dispatchEvent(new Event('change'));
    await settle();
    expect(el.textContent).toContain('Chosen: names.csv.');

    button('Create universe')!.click();
    const post = await nextRequest(http, '/api/universes', 'POST');
    expect(post.request.body).toMatchObject({
      id: 'watch2',
      kind: 'list',
      spec: {},
      csv: 'ticker,start_date\nAAPL.US,2020-01-02\n',
    });
    post.flush({ id: 'watch2', kind: 'list', spec: {} });
    (await nextRequest(http, '/api/universes')).flush(page(UNIVERSES));
    await settle();
  });

  it('imports an index history', async () => {
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    button('Import history')!.click();
    fixture.detectChanges();
    expect(el.textContent).toContain('Enter the index id');
    expect(confirm).not.toHaveBeenCalled();

    type('#ix-id', 'sp500');
    type('#ix-content', 'date,ticker,action\n2024-01-02,AAPL.US,member');
    button('Import history')!.click();
    const post = await nextRequest(http, '/api/universes/index-history', 'POST');
    expect(post.request.body).toEqual({
      index_id: 'sp500',
      format: 'csv',
      content: 'date,ticker,action\n2024-01-02,AAPL.US,member',
    });
    post.flush({ index_id: 'sp500', as_of: '2024-01-02', constituents: 1, changes: 0 });
    await settle();
    expect(success).toHaveBeenCalledWith('Imported 1 members and 0 changes for sp500.');
  });

  it('names universes, with the id only as a detail', () => {
    const titles = [...el.querySelectorAll('[aria-labelledby="list-title"] a')].map((a) =>
      a.textContent?.trim(),
    );
    expect(titles).toContain('US large caps');
    expect(titles).toContain('watch');
    expect(el.textContent).not.toContain('the lake');
  });

  describe('as a viewer', () => {
    beforeAll(() => (me = { ...TRADER, role: 'viewer', scopes: ['read'] }));
    afterAll(() => (me = ADMIN));

    it('sees why New universe and Import history are off', () => {
      expect(button('New universe')!.disabled).toBe(true);
      expect(button('Import history')!.disabled).toBe(true);
      expect(el.textContent).toContain('Traders and admins only.');
    });
  });
});
