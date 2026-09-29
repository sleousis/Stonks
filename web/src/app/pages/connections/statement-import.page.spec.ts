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
import { DEGIRO_PRESETS } from './connections.fixtures';
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

  async function render(imports: unknown[] = [], preset?: string) {
    const fixture = TestBed.createComponent(StatementImportPage);
    if (preset) fixture.componentRef.setInput('preset', preset);
    fixture.detectChanges();
    (await nextRequest(http, '/api/statement-imports')).flush(page(imports));
    (await nextRequest(http, '/api/statement-imports/presets')).flush(DEGIRO_PRESETS);
    await tick();
    fixture.detectChanges();
    return fixture;
  }

  async function pick(
    fixture: ComponentFixture<StatementImportPage>,
    text = CSV,
    name = 'jan.csv',
  ) {
    const root = fixture.nativeElement as HTMLElement;
    const input = root.querySelector('#imp-file') as HTMLInputElement;
    const file = new File([text], name, { type: 'text/csv' });
    Object.defineProperty(input, 'files', { value: [file] });
    input.dispatchEvent(new Event('change'));
    await tick(5);
    const field = root.querySelector('#imp-name') as HTMLInputElement;
    field.value = 'Old broker';
    field.dispatchEvent(new Event('input'));
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

  it('never imports settings other than the previewed ones', async () => {
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    await pick(fixture);
    button(root, 'Preview').click();
    (await nextRequest(http, '/api/statement-imports/preview', 'POST')).flush(PREVIEW);
    await tick();
    fixture.detectChanges();
    const name = root.querySelector('#imp-name') as HTMLInputElement;
    name.value = 'Another broker';
    name.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    button(root, 'Import 1 new rows').click();
    await tick();
    fixture.detectChanges();
    http.expectNone({ method: 'POST', url: '/api/statement-imports' });
    expect(root.textContent).toContain('Preview again');
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

  it('reads a DEGIRO export without a mapping and shows what it left out', async () => {
    const fixture = await render();
    const root = fixture.nativeElement as HTMLElement;
    await pick(fixture, DEGIRO_CSV, 'Transactions.csv');
    button(root, 'Preview').click();
    const pre = await nextRequest(http, '/api/statement-imports/preview', 'POST');
    // "Find out from the headers": no preset and no mapping sent
    expect(pre.request.body.preset).toBeUndefined();
    expect(pre.request.body.mapping).toBeUndefined();
    pre.flush(DEGIRO_PREVIEW);
    await tick();
    fixture.detectChanges();
    expect(root.textContent).toContain('Read as DEGIRO Transactions, Dutch.');
    expect(root.textContent).toContain('Trades are in the account currency');
    expect(root.textContent).toContain('BE0974293251 (ANHEUSER-BUSCH INBEV)');
    // nothing to map
    expect(root.querySelector('#imp-col-date')).toBeNull();
    button(root, 'Import 1 new rows').click();
    const commit = await nextRequest(http, '/api/statement-imports', 'POST');
    expect(commit.request.body.mapping).toBeUndefined();
    commit.flush({ ...IMPORTED, preset: 'degiro_transactions' });
    (await nextRequest(http, '/api/statement-imports')).flush(page([]));
    await tick();
  });

  it('opens with a preset from the link and asks the day of a Portfolio export', async () => {
    const fixture = await render([], 'degiro_portfolio');
    const root = fixture.nativeElement as HTMLElement;
    const select = root.querySelector('#imp-preset') as HTMLSelectElement;
    expect(select.value).toBe('degiro_portfolio');
    expect(root.textContent).toContain('open Portfolio and choose Export');
    await pick(fixture, DEGIRO_CSV, 'Portfolio.csv');
    const day = root.querySelector('#imp-asof') as HTMLInputElement;
    day.value = '2025-06-10';
    day.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    button(root, 'Preview').click();
    const pre = await nextRequest(http, '/api/statement-imports/preview', 'POST');
    expect(pre.request.body).toMatchObject({ preset: 'degiro_portfolio', as_of: '2025-06-10' });
    pre.flush({ ...DEGIRO_PREVIEW, preset: 'degiro_portfolio', kind: 'holdings' });
    await tick();
    fixture.detectChanges();
    // another broker: back to mapping the columns
    select.value = 'none';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(root.querySelector('#imp-asof')).toBeNull();
    button(root, 'Preview').click();
    const again = await nextRequest(http, '/api/statement-imports/preview', 'POST');
    expect(again.request.body.preset).toBe('none');
    again.flush(PREVIEW);
    await tick();
  });
});

const DEGIRO_CSV =
  'Datum,Tijd,Product,ISIN,Beurs,Uitvoeringsplaats,Aantal,Koers,,Lokale waarde,,Waarde EUR,' +
  'Wisselkoers,AutoFX Kosten,Transactiekosten en/of kosten van derden EUR,Totaal EUR,Order ID,\n';

const DEGIRO_PREVIEW: StatementPreview = {
  headers: ['Datum', 'Tijd', 'Product', 'ISIN'],
  mapping: null,
  guessed: false,
  preset: 'degiro_transactions',
  preset_label: 'DEGIRO Transactions',
  locale: 'nl',
  kind: 'activities',
  notes: ['Trades are in the account currency: the price is the value per share.'],
  rows: [
    {
      line: 2,
      status: 'new',
      kind: 'trade',
      day: '2025-03-17',
      symbol: 'BE0974293251',
      ticker: null,
      quantity: 2,
      amount: -114.7,
      currency: 'EUR',
    },
  ],
  total: 1,
  new: 1,
  duplicate: 0,
  skipped: 0,
  first_date: '2025-03-17',
  last_date: '2025-03-17',
  unmapped: ['BE0974293251 (ANHEUSER-BUSCH INBEV)'],
};

const IMPORTED = {
  id: 'imp_2',
  portfolio_id: 'pf_2',
  portfolio_name: 'Old broker',
  filename: 'Transactions.csv',
  rows_total: 1,
  rows_added: 1,
  rows_duplicate: 0,
  rows_skipped: 0,
  first_date: '2025-03-17',
  last_date: '2025-03-17',
  created_at: '2026-09-29T10:00:00Z',
  undone_at: null,
};
