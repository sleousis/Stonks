import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  ColumnMapping,
  StatementImportRequest,
  StatementImportView,
  StatementPreview,
  StatementRowView,
} from '../../api/models';
import { StatementImportsService } from '../../api/statement-imports.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { DataTable, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { type SegmentOption, Segmented } from '../../shared/ui/segmented';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import {
  MAPPED_FIELDS,
  type MappedField,
  cleanMapping,
  textToList,
  textToTypes,
  typesToText,
} from './statement-mapping';

type Target = 'new' | 'existing';

const TARGETS: SegmentOption<Target>[] = [
  { value: 'new', label: 'A new portfolio' },
  { value: 'existing', label: 'An earlier import' },
];

const STATUS_LABELS: Record<string, string> = {
  new: 'New',
  duplicate: 'Already imported',
  skipped: 'Skipped',
};

const ROW_COLUMNS: TableColumn<StatementRowView>[] = [
  { key: 'line', label: 'Line', format: 'number', mobile: 'title' },
  { key: 'status', label: 'Status', display: (r) => STATUS_LABELS[r.status] ?? r.status },
  { key: 'kind', label: 'Kind' },
  { key: 'day', label: 'Date', format: 'date' },
  { key: 'symbol', label: 'Symbol', value: (r) => r.ticker ?? r.symbol ?? null },
  { key: 'quantity', label: 'Quantity', format: 'number' },
  { key: 'amount', label: 'Amount', format: 'signedMoney', currency: (r) => r.currency },
  { key: 'reason', label: 'Why skipped', mobile: 'hide' },
];

/** Rough bytes cap for a file read in the browser (the API allows 5 MB of text). */
const MAX_FILE_BYTES = 5_000_000;

/**
 * CSV statements for brokers without an API (roadmap 23.17). Pick a file,
 * map its columns onto trades, dividends and cash flows, preview every row,
 * then import. The rows land where a broker connection puts them, so
 * insights, cash flows and tax reports read them. Rows already imported are
 * never added twice, and each import can be undone.
 */
@Component({
  selector: 'app-statement-import-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PermissionNote,
    Segmented,
    DataTable,
    EmptyState,
    ErrorState,
    LoadingState,
  ],
  template: `
    <app-page-header
      title="Import a CSV statement"
      description="For brokers without a connection: bring trades, dividends and cash flows in from the CSV file your broker exports."
    >
      <a actions class="btn btn-ghost" routerLink="/connections">Broker connections</a>
    </app-page-header>

    <section class="panel" aria-labelledby="file-title">
      <div class="panel-head"><h2 id="file-title">1. The file</h2></div>
      <div class="panel-body form">
        <div class="field">
          <label for="imp-file">CSV file</label>
          <input
            id="imp-file"
            class="input"
            type="file"
            accept=".csv,text/csv"
            aria-describedby="imp-file-hint"
            (change)="pickFile($any($event.target).files)"
          />
          <span id="imp-file-hint" class="hint">
            @if (filename()) {
              {{ filename() }}, {{ lineCount() }} lines.
            } @else {
              The file stays in your browser until you preview it.
            }
          </span>
        </div>
        <app-segmented
          label="Import into"
          [options]="targets"
          [value]="target()"
          (valueChange)="target.set($event)"
        />
        @if (target() === 'new') {
          <div class="grid-2">
            <div class="field">
              <label for="imp-name">Portfolio name</label>
              <input
                id="imp-name"
                class="input"
                maxlength="80"
                placeholder="e.g. Old broker"
                [value]="newName()"
                (input)="newName.set($any($event.target).value)"
              />
            </div>
            <div class="field">
              <label for="imp-ccy">Currency</label>
              <input
                id="imp-ccy"
                class="input"
                maxlength="3"
                autocapitalize="characters"
                [value]="currency()"
                (input)="currency.set($any($event.target).value.toUpperCase())"
              />
            </div>
          </div>
        } @else {
          <div class="field">
            <label for="imp-portfolio">Portfolio</label>
            <select
              id="imp-portfolio"
              class="input"
              (change)="portfolioId.set($any($event.target).value)"
            >
              <option value="" [selected]="!portfolioId()">Choose one</option>
              @for (p of csvPortfolios(); track p.id) {
                <option [value]="p.id" [selected]="portfolioId() === p.id">{{ p.name }}</option>
              }
            </select>
            @if (csvPortfolios().length === 0) {
              <span class="hint">No earlier imports yet. Start with a new portfolio.</span>
            }
          </div>
        }
      </div>
    </section>

    @if (headers().length) {
      <section class="panel" aria-labelledby="map-title">
        <div class="panel-head">
          <h2 id="map-title">2. Which column is which</h2>
          @if (guessed()) {
            <span class="hint">A first guess from the headers. Check it.</span>
          }
        </div>
        <div class="panel-body form">
          <div class="grid-3">
            @for (f of fields; track f.key) {
              <div class="field">
                <label [for]="'imp-col-' + f.key">{{ f.label }}</label>
                <select
                  class="input"
                  [id]="'imp-col-' + f.key"
                  (change)="setColumn(f.key, $any($event.target).value)"
                >
                  @if (!f.required) {
                    <option value="" [selected]="!column(f.key)">Not in the file</option>
                  }
                  @for (h of headers(); track h) {
                    <option [value]="h" [selected]="column(f.key) === h">{{ h }}</option>
                  }
                </select>
              </div>
            }
          </div>
          <div class="grid-2">
            <div class="field">
              <label for="imp-types">Type values</label>
              <input
                id="imp-types"
                class="input"
                aria-describedby="imp-types-hint"
                [value]="typesText()"
                (input)="typesText.set($any($event.target).value)"
              />
              <span id="imp-types-hint" class="hint"
                >Value=kind, comma separated. Kinds: trade, dividend, interest, fee, deposit,
                withdrawal, split, other.</span
              >
            </div>
            <div class="field">
              <label for="imp-sells">Values that mean a sale</label>
              <input
                id="imp-sells"
                class="input"
                [value]="sellsText()"
                (input)="sellsText.set($any($event.target).value)"
              />
            </div>
            <div class="field">
              <label for="imp-datefmt">Date format</label>
              <input
                id="imp-datefmt"
                class="input"
                placeholder="%d/%m/%Y"
                aria-describedby="imp-datefmt-hint"
                [value]="dateFormat()"
                (input)="dateFormat.set($any($event.target).value)"
              />
              <span id="imp-datefmt-hint" class="hint">Empty tries the common formats.</span>
            </div>
            <div class="field">
              <label for="imp-exchange">Exchange of plain symbols</label>
              <input
                id="imp-exchange"
                class="input"
                placeholder="NASDAQ, LSE"
                autocapitalize="characters"
                [value]="exchange()"
                (input)="exchange.set($any($event.target).value.toUpperCase())"
              />
            </div>
          </div>
          @if (badTypes().length) {
            <p class="error" role="alert">Not understood: {{ badTypes().join(', ') }}</p>
          }
        </div>
      </section>
    }

    <div class="actions">
      <button
        type="button"
        class="btn btn-primary"
        [disabled]="!canPreview() || busy()"
        (click)="preview()"
      >
        {{ busy() === 'preview' ? 'Reading…' : 'Preview' }}
      </button>
      @if (result(); as r) {
        <button
          type="button"
          class="btn"
          [disabled]="!r.new || !!busy() || !canImport()"
          (click)="commit()"
        >
          {{ busy() === 'commit' ? 'Importing…' : 'Import ' + r.new + ' new rows' }}
        </button>
      }
    </div>
    <app-permission-note permission="portfolio.manage" />
    @if (error(); as e) {
      <p class="error" role="alert">{{ e }}</p>
    }

    @if (result(); as r) {
      <section class="panel" aria-labelledby="preview-title">
        <div class="panel-head">
          <h2 id="preview-title">3. Preview</h2>
          <span class="hint" role="status">
            {{ r.new }} new, {{ r.duplicate }} already imported, {{ r.skipped }} skipped
            @if (r.first_date) {
              , {{ day(r.first_date) }} to {{ day(r.last_date) }}
            }
          </span>
        </div>
        @if (r.unmapped.length) {
          <p class="panel-body note">Not covered, kept by symbol: {{ r.unmapped.join(', ') }}</p>
        }
        <app-data-table
          caption="Rows of the statement"
          [rows]="r.rows"
          [columns]="rowColumns"
          [rowKey]="rowKey"
          [pageSize]="25"
        />
      </section>
    }

    <section class="panel" aria-labelledby="done-title">
      <div class="panel-head"><h2 id="done-title">Your imports</h2></div>
      @if (imports.error(); as err) {
        <app-error-state
          title="Could not load your imports"
          [error]="err"
          (retry)="imports.reload()"
        />
      } @else if (!imports.hasValue()) {
        <app-loading-state label="Loading your imports" [rows]="2" />
      } @else if (imports.value().length === 0) {
        <app-empty-state
          title="No imports yet"
          message="Each import shows here, and you can undo it."
        />
      } @else {
        <ul class="panel-body imports">
          @for (i of imports.value(); track i.id) {
            <li class="import" [class.undone]="i.undone_at">
              <div class="import-text">
                <span class="import-name">{{ i.filename || i.id }}</span>
                <span class="hint">
                  {{ i.portfolio_name || i.portfolio_id }}: {{ i.rows_added }} added,
                  {{ i.rows_duplicate }} already there, {{ i.rows_skipped }} skipped.
                  @if (i.undone_at) {
                    Undone {{ day(i.undone_at) }}.
                  }
                </span>
              </div>
              @if (!i.undone_at) {
                <button
                  type="button"
                  class="btn btn-ghost"
                  [attr.aria-label]="'Undo the import of ' + (i.filename || i.id)"
                  [disabled]="!canImport() || !!busy()"
                  (click)="undo(i)"
                >
                  Undo
                </button>
              }
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    :host {
      display: grid;
      gap: var(--space-4);
      min-width: 0;
    }
    .form {
      display: grid;
      gap: var(--space-4);
    }
    .grid-2,
    .grid-3 {
      display: grid;
      gap: var(--space-3);
      grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    }
    .hint,
    .note {
      font-size: var(--text-sm);
      color: var(--color-ink-2);
    }
    .error {
      margin: 0;
      color: var(--color-loss);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .actions .btn,
    .import .btn {
      min-height: 44px;
    }
    .imports {
      display: grid;
      gap: var(--space-2);
      margin: 0;
      list-style: none;
    }
    .import {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      padding-bottom: var(--space-2);
      border-bottom: 1px solid var(--color-border);
    }
    .import.undone .import-name {
      text-decoration: line-through;
    }
    .import-text {
      display: grid;
      gap: 2px;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .import-name {
      font-weight: var(--weight-semibold);
    }
  `,
})
export class StatementImportPage {
  private readonly api = inject(StatementImportsService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  protected readonly targets = TARGETS;
  protected readonly fields = MAPPED_FIELDS;
  protected readonly rowColumns = ROW_COLUMNS;
  protected readonly rowKey = (r: StatementRowView) => String(r.line);

  protected readonly content = signal('');
  protected readonly filename = signal<string | null>(null);
  protected readonly target = signal<Target>('new');
  protected readonly newName = signal('');
  protected readonly currency = signal('USD');
  protected readonly portfolioId = signal('');
  protected readonly headers = signal<string[]>([]);
  protected readonly guessed = signal(false);
  protected readonly mapping = signal<ColumnMapping | null>(null);
  protected readonly typesText = signal('');
  protected readonly sellsText = signal('');
  protected readonly dateFormat = signal('');
  protected readonly exchange = signal('');
  protected readonly result = signal<StatementPreview | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly busy = signal<'preview' | 'commit' | 'undo' | null>(null);

  protected readonly imports = resource({ loader: () => this.api.list() });

  protected readonly lineCount = computed(
    () => this.content().split(/\r?\n/).filter(Boolean).length,
  );
  protected readonly badTypes = computed(() => textToTypes(this.typesText()).bad);
  protected readonly canImport = computed(() => this.session.can('portfolio.manage'));
  protected readonly csvPortfolios = computed(() => {
    const seen = new Map<string, string>();
    for (const i of this.imports.hasValue() ? this.imports.value() : []) {
      if (!seen.has(i.portfolio_id)) seen.set(i.portfolio_id, i.portfolio_name || i.portfolio_id);
    }
    return [...seen].map(([id, name]) => ({ id, name }));
  });
  protected readonly canPreview = computed(() => {
    if (!this.content() || !this.canImport()) return false;
    return this.target() === 'new' ? !!this.newName().trim() : !!this.portfolioId();
  });

  protected column(field: MappedField): string {
    return (this.mapping()?.[field] as string | null | undefined) ?? '';
  }

  protected setColumn(field: MappedField, value: string): void {
    const current = this.mapping();
    if (!current) return;
    const next = { ...current, [field]: value || null } as ColumnMapping;
    if (field === 'type') next.kind = value ? null : 'trade';
    this.mapping.set(next);
    this.result.set(null);
  }

  protected async pickFile(files: FileList | null): Promise<void> {
    const file = files?.[0];
    this.result.set(null);
    this.error.set(null);
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) {
      this.error.set('That file is over 5 MB. Split it into smaller statements.');
      return;
    }
    this.filename.set(file.name);
    this.content.set(await file.text());
    // A fresh file starts from a fresh guess.
    this.mapping.set(null);
    this.headers.set([]);
  }

  private body(): StatementImportRequest {
    const mapping = this.mapping();
    const body: StatementImportRequest = {
      content: this.content(),
      filename: this.filename(),
      currency: this.currency() || 'USD',
      portfolio_id: this.target() === 'existing' ? this.portfolioId() : null,
      new_portfolio: this.target() === 'new' ? this.newName().trim() : null,
    };
    if (mapping) {
      body.mapping = cleanMapping({
        ...mapping,
        types: textToTypes(this.typesText()).types,
        sell_values: textToList(this.sellsText()),
        date_format: this.dateFormat() || null,
        exchange: this.exchange() || null,
      });
    }
    return body;
  }

  protected async preview(): Promise<void> {
    this.busy.set('preview');
    this.error.set(null);
    try {
      const out = await this.api.preview(this.body());
      if (!this.mapping()) {
        this.mapping.set(out.mapping);
        this.typesText.set(typesToText(out.mapping.types));
        this.sellsText.set((out.mapping.sell_values ?? []).join(', '));
        this.dateFormat.set(out.mapping.date_format ?? '');
        this.exchange.set(out.mapping.exchange ?? '');
      }
      this.headers.set(out.headers);
      this.guessed.set(out.guessed);
      this.result.set(out);
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Could not read the statement.');
    } finally {
      this.busy.set(null);
    }
  }

  protected async commit(): Promise<void> {
    this.busy.set('commit');
    this.error.set(null);
    try {
      const done = await this.api.commit(this.body());
      this.toasts.success(
        `${done.rows_added} rows imported into ${done.portfolio_name ?? done.portfolio_id}.`,
        'Statement imported',
      );
      this.target.set('existing');
      this.portfolioId.set(done.portfolio_id);
      this.result.set(null);
      this.imports.reload();
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Could not import the statement.');
    } finally {
      this.busy.set(null);
    }
  }

  protected async undo(i: StatementImportView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Undo the import of ${i.filename || i.id}?`,
      message: `The ${i.rows_added} rows it added go, and the holdings are worked out again. You can import the file again later.`,
      confirmLabel: 'Undo import',
      tone: 'danger',
    });
    if (!ok) return;
    this.busy.set('undo');
    try {
      await this.api.undo(i.id);
      this.imports.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(null);
    }
  }

  protected day(value: string | null | undefined): string {
    return formatDate(value);
  }
}
