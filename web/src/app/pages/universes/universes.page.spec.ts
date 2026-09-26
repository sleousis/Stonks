import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import type { UniverseView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { nextRequest, tick } from '../../../testing/http';
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
    fixture = TestBed.createComponent(UniversesPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    (await nextRequest(http, '/api/universes')).flush(UNIVERSES);
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

  it('creates a universe from a spec and opens it', async () => {
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
    expect(el.querySelector<HTMLTextAreaElement>('#u-spec')!.value).toContain('"exchange"');
    // CSV is for lists only.
    expect(el.querySelector('input[name="u-source"]')).toBeNull();

    button('Create universe')!.click();
    const post = await nextRequest(http, '/api/universes', 'POST');
    expect(post.request.body).toMatchObject({ id: 'tech', kind: 'exchange', csv: null });
    expect(post.request.body.spec).toMatchObject({ exchange: 'US' });
    post.flush({ id: 'tech', kind: 'exchange', spec: {} });
    (await nextRequest(http, '/api/universes')).flush(UNIVERSES);
    await settle();
    expect(navigate).toHaveBeenCalledWith(['/universes', 'tech']);
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
    (await nextRequest(http, '/api/universes')).flush(UNIVERSES);
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
});
