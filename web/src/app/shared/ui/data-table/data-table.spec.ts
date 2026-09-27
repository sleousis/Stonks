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

  it('shows a column through its own display text but sorts on the value', async () => {
    const columns: TableColumn<Row>[] = [
      { key: 'ticker', label: 'Ticker' },
      {
        key: 'value',
        label: 'Value',
        format: 'number',
        display: (r) => `${r.value} units`,
      },
    ];
    const { fixture, el } = await render({ columns });
    const valueCells = () =>
      [...el.querySelectorAll('tbody tr')].map((tr) =>
        tr.querySelectorAll('td')[1].textContent?.trim(),
      );
    expect(valueCells()).toEqual(['2 units', '10 units', '–']);
    el.querySelectorAll<HTMLButtonElement>('th button')[1].click();
    await fixture.whenStable();
    // Numbers sort high first: 10 before 2, not "2 units" before "10 units".
    expect(valueCells()[0]).toBe('10 units');
  });

  it('names its scroll region apart from the panel heading it repeats (A11Y-2)', async () => {
    const { el } = await render({ caption: 'Stored universes' });
    const region = el.querySelector('[role="region"]');
    expect(region?.getAttribute('aria-label')).toBe('Stored universes, scrollable table');
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

  it('server mode shows the page given by the offset input', async () => {
    // A page re-creates the table after each load: the offset keeps the pager right.
    const { fixture, el } = await render({ pageSize: 2, total: 7, offset: 4 });
    const pages: unknown[] = [];
    fixture.componentInstance.pageChange.subscribe((p) => pages.push(p));
    expect(el.textContent).toContain('5–6 of 7');
    const button = (text: string) =>
      [...el.querySelectorAll<HTMLButtonElement>('.pager button')].find(
        (b) => b.textContent?.trim() === text,
      )!;
    expect(button('Previous').disabled).toBe(false);
    button('Next').click();
    await fixture.whenStable();
    expect(pages).toEqual([{ offset: 6, limit: 2 }]);

    fixture.componentRef.setInput('offset', 6);
    await fixture.whenStable();
    expect(el.textContent).toContain('7–7 of 7');
    expect(button('Next').disabled).toBe(true);
  });

  it('server mode headers are not sort buttons and rows keep the API order', async () => {
    const { el, firstCells } = await render({
      pageSize: 2,
      total: 7,
      initialSort: { key: 'ticker', dir: 'asc' },
    });
    expect(el.querySelectorAll('th button.sort').length).toBe(0);
    expect(el.querySelector('th')?.getAttribute('aria-sort')).toBeNull();
    expect(firstCells()).toEqual(['b', 'a', 'c']);
  });

  it('keeps rows on screen and shows a progress bar while busy', async () => {
    const { fixture, el } = await render({ busy: true });
    expect(el.querySelector('.busy-bar')).not.toBeNull();
    expect(el.querySelector('.table-wrap')?.getAttribute('aria-busy')).toBe('true');
    expect(el.querySelectorAll('tbody tr').length).toBe(3);
    fixture.componentRef.setInput('busy', false);
    await fixture.whenStable();
    expect(el.querySelector('.busy-bar')).toBeNull();
  });

  it('shows the empty message', async () => {
    const { el } = await render({ rows: [], emptyMessage: 'Nothing here.' });
    expect(el.querySelector('tbody')?.textContent).toContain('Nothing here.');
  });
});
