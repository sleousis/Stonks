import { NgTemplateOutlet } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  Directive,
  TemplateRef,
  computed,
  contentChildren,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';

import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  toneClass,
} from '../../../core/format/format';
import { HelpTip } from '../help-tip';

export type CellFormat =
  'text' | 'number' | 'money' | 'signedMoney' | 'percent' | 'signedPercent' | 'date' | 'datetime';

export type CellValue = string | number | boolean | null | undefined;

export interface TableColumn<T> {
  /** Unique id; also the row property read when `value` is omitted. */
  key: string;
  label: string;
  /** Accessor used for display and sorting. */
  value?: (row: T) => CellValue;
  format?: CellFormat;
  /** Defaults to `end` for numeric formats. */
  align?: 'start' | 'end';
  /** Defaults to true. */
  sortable?: boolean;
  /** Colour numbers by sign (gain/loss). */
  tone?: boolean;
  /** Money columns: the row's currency (ISO 4217); USD when missing. */
  currency?: (row: T) => string | null | undefined;
  /**
   * Phone card layout: `title` is the card heading (use it for the key
   * column, e.g. ticker or id), `hide` drops the column on phones, and the
   * default shows a label/value line.
   */
  mobile?: 'title' | 'show' | 'hide';
  /**
   * Glossary key for the header's help tip. Defaults to the label, so metric
   * columns ("Sharpe", "Drawdown") get one automatically; `false` hides it.
   */
  help?: string | false;
}

export interface SortState {
  key: string;
  dir: 'asc' | 'desc';
}

export interface PageRequest {
  offset: number;
  limit: number;
}

export interface TableCellContext<T> {
  $implicit: T;
}

/**
 * Custom cell template for a column key; the row is the implicit context.
 * Pass the same rows to `appCellOf` so `row` is typed in the template.
 *
 *   <ng-template appCell="status" [appCellOf]="strategies" let-row>
 *     <app-status-pill [status]="row.status" />
 *   </ng-template>
 */
@Directive({ selector: 'ng-template[appCell]' })
export class TableCell<T = unknown> {
  readonly column = input.required<string>({ alias: 'appCell' });
  /** Only used for template type-checking of `let-row`. */
  readonly appCellOf = input<readonly T[]>([]);
  readonly template = inject<TemplateRef<TableCellContext<unknown>>>(TemplateRef);

  static ngTemplateContextGuard<T>(
    _dir: TableCell<T>,
    // eslint-disable-next-line @typescript-eslint/no-unused-vars -- type guard signature
    ctx: unknown,
  ): ctx is TableCellContext<T> {
    return true;
  }
}

const NUMERIC: readonly CellFormat[] = [
  'number',
  'money',
  'signedMoney',
  'percent',
  'signedPercent',
];

/**
 * Sortable, paginated table that turns into stacked cards on phones.
 *
 * Client mode (default): pass all rows; sorting and paging happen here.
 * Server mode: pass `total` (the API page's `total`) and `offset`, and handle
 * `pageChange` by refetching with `{ offset, limit }`. Rows stay in the API's
 * order and the headers are not sort buttons: sorting one page of 50 rows
 * would claim an order the other pages do not follow.
 */
@Component({
  selector: 'app-data-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgTemplateOutlet, HelpTip],
  templateUrl: './data-table.html',
  styleUrl: './data-table.scss',
})
export class DataTable<T extends object> {
  readonly rows = input.required<readonly T[]>();
  readonly columns = input.required<readonly TableColumn<T>[]>();
  /** Describes the table for screen readers (visually hidden). */
  readonly caption = input.required<string>();
  readonly rowKey = input<(row: T) => string>();
  /** Rows per page; 0 shows all rows. */
  readonly pageSize = input(20);
  readonly initialSort = input<SortState | null>(null);
  /** Server mode: total rows across all pages. */
  readonly total = input<number | null>(null);
  /**
   * Server mode: the offset of the rows shown (the API page's `offset`). The
   * pager reads its page from it, so a table re-created after a load still
   * shows the right range.
   */
  readonly offset = input<number | null>(null);
  readonly emptyMessage = input('No rows to show.');
  /**
   * The next page is loading while these rows stay on screen: dims them and
   * shows a thin progress bar (see `keepLatest()`).
   */
  readonly busy = input(false);
  readonly pageChange = output<PageRequest>();

  protected readonly cells = contentChildren(TableCell);
  protected readonly cellTemplates = computed(() => {
    const map = new Map<string, TemplateRef<TableCellContext<unknown>>>();
    for (const c of this.cells()) map.set(c.column(), c.template);
    return map;
  });

  protected readonly sort = linkedSignal<SortState | null>(() => this.initialSort());
  protected readonly page = linkedSignal<{ rows: readonly T[]; offset: number | null }, number>({
    source: () => ({ rows: this.rows(), offset: this.offset() }),
    // Client mode: new rows start from page one. Server mode: the page comes
    // from the offset when given, else it stays where it was.
    computation: ({ offset }, prev) => {
      if (this.total() === null) return 0;
      const size = this.pageSize();
      if (offset !== null && size > 0) return Math.floor(offset / size);
      return prev ? prev.value : 0;
    },
  });
  protected readonly liveMessage = signal('');

  protected readonly sortedRows = computed(() => {
    const rows = this.rows();
    const sort = this.sort();
    if (!sort || this.serverMode()) return rows;
    const col = this.columns().find((c) => c.key === sort.key);
    if (!col) return rows;
    const factor = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => factor * compare(this.raw(a, col), this.raw(b, col)));
  });

  protected readonly serverMode = computed(() => this.total() !== null);
  protected readonly totalRows = computed(() => this.total() ?? this.rows().length);
  protected readonly pageCount = computed(() => {
    const size = this.pageSize();
    return size > 0 ? Math.max(1, Math.ceil(this.totalRows() / size)) : 1;
  });

  protected readonly visibleRows = computed(() => {
    const size = this.pageSize();
    const rows = this.sortedRows();
    if (size <= 0 || this.total() !== null) return rows;
    const start = this.page() * size;
    return rows.slice(start, start + size);
  });

  protected readonly rangeLabel = computed(() => {
    const total = this.totalRows();
    const size = this.pageSize();
    if (total === 0) return '0 rows';
    if (size <= 0) return `${total} rows`;
    const start = this.page() * size + 1;
    const end = Math.min(total, start + size - 1);
    return `${start}–${end} of ${total}`;
  });

  protected isNumeric(col: TableColumn<T>): boolean {
    return !!col.format && NUMERIC.includes(col.format);
  }

  /** Numbers, dates and times are set in the mono figure face. */
  protected isFigure(col: TableColumn<T>): boolean {
    return this.isNumeric(col) || col.format === 'date' || col.format === 'datetime';
  }

  protected alignEnd(col: TableColumn<T>): boolean {
    return (col.align ?? (this.isNumeric(col) ? 'end' : 'start')) === 'end';
  }

  protected helpTerm(col: TableColumn<T>): string | null {
    return col.help === false ? null : (col.help ?? col.label);
  }

  protected sortable(col: TableColumn<T>): boolean {
    return !this.serverMode() && col.sortable !== false;
  }

  protected ariaSort(col: TableColumn<T>): 'ascending' | 'descending' | null {
    if (this.serverMode()) return null;
    const sort = this.sort();
    if (!sort || sort.key !== col.key) return null;
    return sort.dir === 'asc' ? 'ascending' : 'descending';
  }

  protected toggleSort(col: TableColumn<T>): void {
    const current = this.sort();
    // Numbers sort high-to-low first; text A-Z first.
    const first: SortState['dir'] = this.isNumeric(col) ? 'desc' : 'asc';
    const dir: SortState['dir'] =
      current?.key === col.key ? (current.dir === 'asc' ? 'desc' : 'asc') : first;
    this.sort.set({ key: col.key, dir });
    this.liveMessage.set(`Sorted by ${col.label}, ${dir === 'asc' ? 'ascending' : 'descending'}`);
  }

  protected goTo(page: number): void {
    const next = Math.min(Math.max(0, page), this.pageCount() - 1);
    if (next === this.page()) return;
    this.page.set(next);
    const size = this.pageSize();
    this.pageChange.emit({ offset: next * size, limit: size });
  }

  protected display(row: T, col: TableColumn<T>): string {
    const value = this.raw(row, col);
    if (value === null || value === undefined || value === '') return '–';
    switch (col.format) {
      case 'money':
        return formatMoney(value as number, { currency: col.currency?.(row) });
      case 'signedMoney':
        return formatMoney(value as number, { signed: true, currency: col.currency?.(row) });
      case 'percent':
        return formatPercent(value as number);
      case 'signedPercent':
        return formatPercent(value as number, { signed: true });
      case 'number':
        return formatNumber(value as number);
      case 'date':
        return formatDate(String(value));
      case 'datetime':
        return formatDateTime(String(value));
      default:
        return typeof value === 'boolean' ? (value ? 'Yes' : 'No') : String(value);
    }
  }

  protected tone(row: T, col: TableColumn<T>): string {
    if (!col.tone) return '';
    const value = this.raw(row, col);
    return typeof value === 'number' ? toneClass(value) : '';
  }

  protected trackRow = (index: number, row: T): unknown => this.rowKey()?.(row) ?? row;

  private raw(row: T, col: TableColumn<T>): CellValue {
    if (col.value) return col.value(row);
    return (row as Record<string, CellValue>)[col.key];
  }
}

function compare(a: CellValue, b: CellValue): number {
  // Missing values always sort last-ish (treated as smallest).
  const aMissing = a === null || a === undefined || a === '';
  const bMissing = b === null || b === undefined || b === '';
  if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? -1 : 1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}
