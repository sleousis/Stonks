import { TestBed } from '@angular/core/testing';

import { DataTable, type TableColumn } from './data-table';

interface Row {
  ticker: string;
  value: number | null;
}

const ROWS: Row[] = [
  { ticker: 'b', value: 2 },
  { ticker: 'a', value: 10 },
  { ticker: 'c', value: null },
];
const COLUMNS: TableColumn<Row>[] = [
  { key: 'ticker', label: 'Ticker', mobile: 'title' },
  { key: 'value', label: 'Value', format: 'money', tone: true },
];

async function render(inputs: Record<string, unknown>) {
  const fixture = TestBed.createComponent(DataTable<Row>);
  fixture.componentRef.setInput('rows', ROWS);
  fixture.componentRef.setInput('columns', COLUMNS);
  fixture.componentRef.setInput('caption', 'Test table');
  for (const [k, v] of Object.entries(inputs)) fixture.componentRef.setInput(k, v);
  await fixture.whenStable();
  const el: HTMLElement = fixture.nativeElement;
  const firstCells = () =>
    [...el.querySelectorAll('tbody tr')].map((tr) => tr.querySelector('td')?.textContent?.trim());
  return { fixture, el, firstCells };
}

describe('DataTable', () => {
  it('renders formatted cells with labels for the phone card layout', async () => {
    const { el } = await render({});
    const cells = el.querySelectorAll('tbody tr:first-child td');
    expect(cells[1].textContent?.trim()).toBe('$2.00');
    expect(cells[1].getAttribute('data-label')).toBe('Value');
    expect(cells[1].classList).toContain('gain');
    expect(cells[0].classList).toContain('cell-title');
    expect(el.querySelector('caption')?.textContent).toContain('Test table');
  });

  it('sorts on header click and exposes aria-sort', async () => {
    const { fixture, el, firstCells } = await render({});
    const [tickerBtn, valueBtn] = el.querySelectorAll<HTMLButtonElement>('th button');

    tickerBtn.click();
    await fixture.whenStable();
    expect(firstCells()).toEqual(['a', 'b', 'c']);
    expect(el.querySelector('th')?.getAttribute('aria-sort')).toBe('ascending');

    tickerBtn.click();
    await fixture.whenStable();
    expect(firstCells()).toEqual(['c', 'b', 'a']);

    // Numbers start high-to-low; missing values sort as smallest.
    valueBtn.click();
    await fixture.whenStable();
    expect(firstCells()).toEqual(['a', 'b', 'c']);
  });

  it('paginates client-side', async () => {
    const { fixture, el, firstCells } = await render({ pageSize: 2 });
    expect(firstCells()).toEqual(['b', 'a']);
    expect(el.textContent).toContain('1–2 of 3');
    const next = [...el.querySelectorAll<HTMLButtonElement>('.pager button')].find(
      (b) => b.textContent?.trim() === 'Next',
    )!;
    next.click();
    await fixture.whenStable();
    expect(firstCells()).toEqual(['c']);
    expect(next.disabled).toBe(true);
  });

  it('emits page requests in server mode', async () => {
    const { fixture, el } = await render({ pageSize: 2, total: 7 });
    const pages: unknown[] = [];
    fixture.componentInstance.pageChange.subscribe((p) => pages.push(p));
    expect(el.textContent).toContain('1–2 of 7');
    [...el.querySelectorAll<HTMLButtonElement>('.pager button')]
      .find((b) => b.textContent?.trim() === 'Next')!
      .click();
    await fixture.whenStable();
    expect(pages).toEqual([{ offset: 2, limit: 2 }]);
  });

  it('shows the empty message', async () => {
    const { el } = await render({ rows: [], emptyMessage: 'Nothing here.' });
    expect(el.querySelector('tbody')?.textContent).toContain('Nothing here.');
  });
});
