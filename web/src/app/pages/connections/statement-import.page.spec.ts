import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import type { ComponentFixture } from '@angular/core/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { StatementPreview } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { TRADER, problem } from '../../../testing/auth-fixtures';
import { nextRequest, page, tick } from '../../../testing/http';
import { StatementImportPage } from './statement-import.page';

const CSV = 'Date,Action,Symbol,Quantity,Amount\n2026-01-05,BUY,AAPL,10,-1900\n';

const PREVIEW: StatementPreview = {
  headers: ['Date', 'Action', 'Symbol', 'Quantity', 'Amount'],
  mapping: {
    date: 'Date',
    type: 'Action',
    symbol: 'Symbol',
    quantity: 'Quantity',
    amount: 'Amount',
    types: { BUY: 'trade' },
    sell_values: ['SELL'],
    default_currency: 'USD',
  },
  guessed: true,
  rows: [
    {
      line: 2,
      status: 'new',
      kind: 'trade',
      day: '2026-01-05',
      symbol: 'AAPL',
      ticker: 'AAPL.US',
      quantity: 10,
      amount: -1900,
      currency: 'USD',
    },
  ],
  total: 1,
  new: 1,
  duplicate: 0,
  skipped: 0,
  first_date: '2026-01-05',
  last_date: '2026-01-05',
  unmapped: [],
};

describe('StatementImportPage', () => {
  let http: HttpTestingController;
  const confirm = vi.fn();

  beforeEach(async () => {
    confirm.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ConfirmService, useValue: { confirm } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(http, '/api/auth/me')).flush(TRADER);
    await loading;
  });

  afterEach(() => http.verify());

  async function render(imports: unknown[] = []) {
    const fixture = TestBed.createComponent(StatementImportPage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/statement-imports')).flush(page(imports));
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  async function pick(fixture: ComponentFixture<StatementImportPage>) {
    const root = fixture.nativeElement as HTMLElement;
    const input = root.querySelector('#imp-file') as HTMLInputElement;
    const file = new File([CSV], 'jan.csv', { type: 'text/csv' });
    Object.defineProperty(input, 'files', { value: [file] });
    input.dispatchEvent(new Event('change'));
    await tick(5);
    const name = root.querySelector('#imp-name') as HTMLInputElement;
    name.value = 'Old broker';
    name.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function button(root: HTMLElement, text: string): HTMLButtonElement {
    const b = [...root.querySelectorAll('button')].find((x) => x.textContent?.includes(text));
    if (!b) throw new Error(`no "${text}" button`);
    return b;
  }

  it('previews with a guessed mapping, then imports the new rows', async () => {
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    await pick(fixture);
    button(root, 'Preview').click();
    const pre = await nextRequest(http, '/api/statement-imports/preview', 'POST');
    expect(pre.request.body).toMatchObject({
      content: CSV,
      filename: 'jan.csv',
      new_portfolio: 'Old broker',
    });
    expect(pre.request.body.mapping).toBeUndefined();
    pre.flush(PREVIEW);
    await tick();
    fixture.detectChanges();
    expect(root.textContent).toContain('A first guess from the headers');
    expect(root.textContent).toContain('1 new, 0 already imported, 0 skipped');
    expect((root.querySelector('#imp-col-symbol') as HTMLSelectElement).value).toBe('Symbol');

    button(root, 'Import 1 new rows').click();
    const commit = await nextRequest(http, '/api/statement-imports', 'POST');
    expect(commit.request.body.mapping).toMatchObject({ date: 'Date', types: { BUY: 'trade' } });
    commit.flush({
      id: 'imp_1',
      portfolio_id: 'pf_1',
      portfolio_name: 'Old broker',
      filename: 'jan.csv',
      rows_total: 1,
      rows_added: 1,
      rows_duplicate: 0,
      rows_skipped: 0,
      first_date: '2026-01-05',
      last_date: '2026-01-05',
      created_at: '2026-09-28T10:00:00Z',
      undone_at: null,
    });
    (await nextRequest(http, '/api/statement-imports')).flush(
      page([
        {
          id: 'imp_1',
          portfolio_id: 'pf_1',
          portfolio_name: 'Old broker',
          filename: 'jan.csv',
          rows_total: 1,
          rows_added: 1,
          rows_duplicate: 0,
          rows_skipped: 0,
          first_date: '2026-01-05',
          last_date: '2026-01-05',
          created_at: '2026-09-28T10:00:00Z',
          undone_at: null,
        },
      ]),
    );
    await tick();
    fixture.detectChanges();
    expect(root.textContent).toContain('Old broker: 1 added');
  });

  it('shows a refusal where the trader is looking', async () => {
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    await pick(fixture);
    button(root, 'Preview').click();
    (await nextRequest(http, '/api/statement-imports/preview', 'POST')).flush(
      problem(422, "no column named 'When' in the CSV"),
      { status: 422, statusText: 'Unprocessable Entity' },
    );
    await tick();
    fixture.detectChanges();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain("no column named 'When'");
  });

  it('undoes an import after asking', async () => {
    confirm.mockResolvedValue(true);
    const fixture = await render([
      {
        id: 'imp_1',
        portfolio_id: 'pf_1',
        portfolio_name: 'Old broker',
        filename: 'jan.csv',
        rows_total: 1,
        rows_added: 1,
        rows_duplicate: 0,
        rows_skipped: 0,
        first_date: null,
        last_date: null,
        created_at: '2026-09-28T10:00:00Z',
        undone_at: null,
      },
    ]);
    const root = fixture.nativeElement as HTMLElement;
    (root.querySelector('button[aria-label="Undo the import of jan.csv"]') as HTMLElement).click();
    await tick();
    (await nextRequest(http, '/api/statement-imports/imp_1/undo', 'POST')).flush({});
    (await nextRequest(http, '/api/statement-imports')).flush(page([]));
    await tick();
    fixture.detectChanges();
    expect(confirm).toHaveBeenCalledOnce();
    expect(root.textContent).toContain('No imports yet');
  });
});
