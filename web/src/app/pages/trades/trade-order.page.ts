import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { JournalEntryView, JournalNoteView } from '../../api/models';
import { TcaService } from '../../api/tca.service';
import { SessionService } from '../../core/auth/session.service';
import { formatMoney, formatNumber } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { PortfolioContextService } from '../../core/portfolio/portfolio-context.service';
import { DateTimePipe, MoneyPipe, NumPipe } from '../../shared/format.pipes';
import { ModeStamp } from '../../shared/ui/mode-stamp';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { SideTag } from '../../shared/ui/side-tag';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';
import { humanize } from '../../shared/ui/param-form/param-spec';
import { formatBps, portfolioLive, portfolioName, triggerLabel } from './trades-format';

const NOTE_MAX = 4000;

interface CostLine {
  label: string;
  hint: string;
  bps: string;
  money?: string;
  total?: boolean;
}

/**
 * One order as a ticket: side, ticker, quantity, decision price against the
 * fill, the shortfall broken into its parts, why it was placed, and the
 * journal notes (add and edit your own with `portfolio.manage`).
 */
@Component({
  selector: 'app-trade-order-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    SideTag,
    ModeStamp,
    StatusPill,
    PermissionNote,
    EmptyState,
    ErrorState,
    LoadingState,
    MoneyPipe,
    NumPipe,
    DateTimePipe,
  ],
  templateUrl: './trade-order.page.html',
  styleUrl: './trade-order.page.scss',
})
export class TradeOrderPage {
  /** From the route `/trades/orders/:clientId`. */
  readonly clientId = input.required<string>();

  private readonly tca = inject(TcaService);
  private readonly toasts = inject(ToastService);
  private readonly ctx = inject(PortfolioContextService);
  protected readonly session = inject(SessionService);

  protected readonly noteMax = NOTE_MAX;

  protected readonly order = resource({
    params: () => ({ id: this.clientId() }),
    loader: ({ params }) => this.tca.order(params.id),
  });

  protected readonly entry = computed<JournalEntryView | null>(() =>
    this.order.hasValue() ? this.order.value() : null,
  );

  protected readonly heading = computed(() => {
    const e = this.entry();
    if (!e) return 'Order';
    const side = e.side.toLowerCase() === 'sell' ? 'Sell' : 'Buy';
    return `${side} ${formatNumber(e.quantity)} ${e.ticker}`;
  });

  /** Paper or live, when the portfolio list says. */
  protected readonly live = computed(() =>
    portfolioLive(this.entry()?.portfolio_id, this.ctx.options()),
  );
  protected readonly portfolio = computed(() =>
    portfolioName(this.entry()?.portfolio_id, this.ctx.options()),
  );

  protected readonly costLines = computed<CostLine[]>(() => {
    const s = this.entry()?.shortfall;
    if (!s) return [];
    return [
      {
        label: 'Market move before the order arrived',
        hint: 'Decision price to arrival price',
        bps: formatBps(s.delay_bps),
      },
      {
        label: 'Spread and price impact',
        hint: 'Arrival price to fill price',
        bps: formatBps(s.impact_bps),
      },
      { label: 'Fees', hint: 'Charged by the broker', bps: formatBps(s.fee_bps) },
      {
        label: 'Shortfall',
        hint: 'All of the above',
        bps: formatBps(s.is_bps),
        money: formatMoney(s.is_cost),
        total: true,
      },
      {
        label: 'Missed fills',
        hint: 'What the unfilled part would have made or lost by the next close',
        bps: formatBps(s.opportunity_bps),
        money: formatMoney(s.opportunity_cost),
      },
      {
        label: 'Total cost',
        hint: 'Shortfall plus missed fills, over the whole order',
        bps: formatBps(s.total_bps),
        total: true,
      },
    ];
  });

  /** Primitive context values (score, rank, target weight...) as label and text. */
  protected readonly contextRows = computed(() => {
    const ctx = this.entry()?.context ?? {};
    return Object.entries(ctx)
      .filter(([key, v]) => key !== 'trigger' && v !== null && typeof v !== 'object')
      .map(([key, v]) => ({
        label: humanize(key),
        value: typeof v === 'number' ? formatNumber(v) : String(v),
      }));
  });

  protected readonly canWrite = computed(() => this.session.can('portfolio.manage'));
  private readonly actor = computed(() => {
    const id = this.session.me()?.user_id;
    return id ? `user:${id}` : null;
  });

  protected readonly draft = signal('');
  protected readonly adding = signal(false);
  protected readonly editingId = signal<number | null>(null);
  protected readonly editText = signal('');
  protected readonly saving = signal(false);

  protected isMine(note: JournalNoteView): boolean {
    return this.actor() !== null && note.author === this.actor();
  }

  protected authorLabel(note: JournalNoteView): string {
    if (this.isMine(note)) return 'You';
    return note.author.startsWith('service:') ? 'Automation' : 'Another user';
  }

  protected trigger(value: string | null | undefined): string {
    return triggerLabel(value);
  }

  protected bps(value: number | null | undefined): string {
    return formatBps(value);
  }

  async addNote(): Promise<void> {
    const text = this.draft().trim();
    const e = this.entry();
    if (!text || !e || this.adding()) return;
    this.adding.set(true);
    try {
      const note = await this.tca.addNote(e.client_id, text);
      this.order.update((v) => (v ? { ...v, notes: [...v.notes, note] } : v));
      this.draft.set('');
      this.toasts.success('Added the note.');
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.adding.set(false);
    }
  }

  startEdit(note: JournalNoteView): void {
    this.editingId.set(note.id);
    this.editText.set(note.note);
  }

  cancelEdit(): void {
    this.editingId.set(null);
  }

  async saveEdit(note: JournalNoteView): Promise<void> {
    const text = this.editText().trim();
    if (!text || this.saving()) return;
    this.saving.set(true);
    try {
      const saved = await this.tca.editNote(note.id, text);
      this.order.update((v) =>
        v ? { ...v, notes: v.notes.map((n) => (n.id === saved.id ? saved : n)) } : v,
      );
      this.editingId.set(null);
      this.toasts.success('Saved the note.');
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.saving.set(false);
    }
  }
}
