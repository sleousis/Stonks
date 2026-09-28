import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';

import { LiveService } from '../../api/live.service';
import type { LivePreviewView, PreviewOrderView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { formatDate, formatMoney } from '../../core/format/format';
import { stageWords } from '../../shared/live-stages';
import { rememberPreview } from '../going-live/preview-memory';
import { DataTable, TableCell, type TableColumn } from '../../shared/ui/data-table/data-table';
import { PermissionNote } from '../../shared/ui/permission-note';
import { SideTag } from '../../shared/ui/side-tag';
import { ErrorState, LoadingState } from '../../shared/ui/states';

/** What the dry run made of the book, in plain words. */
export function previewOutcome(p: Pick<LivePreviewView, 'status' | 'orders' | 'reason'>): string {
  if (p.status === 'error') return `The dry run failed: ${p.reason ?? 'no reason given'}.`;
  if (!p.orders.length) {
    return p.reason ? `Nothing to send: ${p.reason.replaceAll('_', ' ')}.` : 'Nothing to send.';
  }
  const n = p.orders.length;
  return `${n} order${n === 1 ? '' : 's'} would go out.`;
}

/** The broker's commission for an order, or why it is unknown. */
export function commissionText(o: PreviewOrderView): string {
  if (o.what_if_error) return `Check failed: ${o.what_if_error}`;
  const w = o.what_if;
  if (!w || w.commission == null) return 'Not given';
  return formatMoney(w.commission, { currency: w.commission_currency ?? 'USD' });
}

/**
 * The live dry-run preview (roadmap 19.9): the orders the live book would
 * send now, after every risk rule, live safeguard and account rule, with
 * the broker's own cost and margin check. It never sends an order, so it
 * needs trade rights and no code.
 */
@Component({
  selector: 'app-live-preview-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DataTable, TableCell, PermissionNote, SideTag, ErrorState, LoadingState],
  templateUrl: './live-preview-panel.html',
  styleUrl: './live-preview-panel.scss',
})
export class LivePreviewPanel {
  private readonly live = inject(LiveService);
  private readonly session = inject(SessionService);

  readonly portfolioId = input.required<string>();

  protected readonly canTrade = computed(() => this.session.can('portfolio.trade'));
  protected readonly running = signal(false);
  protected readonly result = signal<LivePreviewView | null>(null);
  protected readonly failed = signal<unknown>(null);
  protected readonly outcome = previewOutcome;
  protected readonly date = formatDate;
  protected readonly words = stageWords;
  protected readonly money = formatMoney;

  protected readonly dropped = computed(() =>
    (this.result()?.adjustments ?? []).filter((a) => a.adjusted_quantity <= 0),
  );

  protected readonly columns: readonly TableColumn<PreviewOrderView>[] = [
    { key: 'ticker', label: 'Ticker', mobile: 'title' },
    { key: 'side', label: 'Side' },
    { key: 'quantity', label: 'Quantity', format: 'number' },
    { key: 'limit_price', label: 'Limit', format: 'number', help: false },
    {
      key: 'notional',
      label: 'Value',
      format: 'money',
      help: false,
      currency: () => this.result()?.account?.currency ?? 'USD',
    },
    { key: 'commission', label: 'Broker fee', value: commissionText, help: false },
    {
      key: 'margin',
      label: 'Margin change',
      value: (o) => o.what_if?.initial_margin_change ?? null,
      format: 'number',
      help: false,
    },
    {
      key: 'rules',
      label: 'Rules that changed it',
      value: (o) => [...new Set(o.adjustments.map((a) => a.rule))].join(', ') || 'None',
      sortable: false,
      help: false,
    },
  ];
  protected readonly orderKey = (o: PreviewOrderView) => o.client_id;

  async run(): Promise<void> {
    if (this.running() || !this.canTrade()) return;
    this.running.set(true);
    this.failed.set(null);
    try {
      const result = await this.live.preview(this.portfolioId());
      this.result.set(result);
      // The Going live checklist counts a preview that ran as its step done.
      if (result.status !== 'error') rememberPreview(this.portfolioId());
    } catch (err) {
      this.failed.set(err);
    } finally {
      this.running.set(false);
    }
  }
}
