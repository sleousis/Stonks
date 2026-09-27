import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { WatchlistView } from '../../api/models';
import { WatchlistsService } from '../../api/watchlists.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ApiError } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { WatchlistContextService } from '../../core/watchlists/watchlist-context.service';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';
import { parseTickerText } from '../welcome/welcome-steps';

/** `/lab?tickers=A,B`: the lab forms start from these tickers. */
export function labLink(w: Pick<WatchlistView, 'tickers'>): { tickers: string } {
  return { tickers: w.tickers.join(',') };
}

/**
 * Your watchlists: named ticker lists. Each opens in the lab as a quick
 * universe, each ticker opens its chart, and Today and the charts can show
 * only one list.
 */
@Component({
  selector: 'app-watchlists-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, PageHeader, PermissionNote, LoadingState, EmptyState, ErrorState],
  templateUrl: './watchlists.page.html',
  styleUrl: './watchlists.page.scss',
})
export class WatchlistsPage {
  private readonly api = inject(WatchlistsService);
  private readonly ctx = inject(WatchlistContextService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  protected readonly session = inject(SessionService);

  protected readonly lists = resource({ loader: () => this.api.list() });
  protected readonly canEdit = computed(() => this.session.can('portfolio.manage'));

  protected readonly newName = signal('');
  protected readonly newTickers = signal('');
  protected readonly newError = signal<string | null>(null);
  protected readonly creating = signal(false);

  /** The list being edited, with its draft name and tickers. */
  protected readonly editing = signal<{ id: string; name: string; tickers: string } | null>(null);
  protected readonly editError = signal<string | null>(null);
  protected readonly saving = signal(false);

  protected readonly labLink = labLink;

  protected async create(event: Event): Promise<void> {
    event.preventDefault();
    const name = this.newName().trim();
    const tickers = parseTickerText(this.newTickers());
    if (!name) return this.newError.set('Give the watchlist a name.');
    this.creating.set(true);
    this.newError.set(null);
    try {
      const made = await this.api.create({ name, tickers });
      this.newName.set('');
      this.newTickers.set('');
      this.toasts.success(`Saved ${made.name}.`);
      this.refresh();
    } catch (err) {
      if (err instanceof ApiError) this.newError.set(err.message);
    } finally {
      this.creating.set(false);
    }
  }

  protected edit(w: WatchlistView): void {
    this.editError.set(null);
    this.editing.set({ id: w.id, name: w.name, tickers: w.tickers.join(', ') });
  }

  protected patchEdit(patch: Partial<{ name: string; tickers: string }>): void {
    this.editing.update((e) => (e ? { ...e, ...patch } : e));
  }

  protected async save(event: Event): Promise<void> {
    event.preventDefault();
    const e = this.editing();
    if (!e) return;
    const name = e.name.trim();
    if (!name) return this.editError.set('Give the watchlist a name.');
    this.saving.set(true);
    this.editError.set(null);
    try {
      const saved = await this.api.update(e.id, { name, tickers: parseTickerText(e.tickers) });
      this.editing.set(null);
      this.toasts.success(`Saved ${saved.name}.`);
      this.refresh();
    } catch (err) {
      if (err instanceof ApiError) this.editError.set(err.message);
    } finally {
      this.saving.set(false);
    }
  }

  protected async remove(w: WatchlistView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Delete ${w.name}?`,
      message: 'The list goes for good. Your trades and strategies do not change.',
      confirmLabel: 'Delete watchlist',
      tone: 'danger',
    });
    if (!ok) return;
    try {
      await this.api.delete(w.id);
      if (this.ctx.selectedId() === w.id) this.ctx.select(null);
      this.toasts.success(`Deleted ${w.name}.`);
      this.refresh();
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  private refresh(): void {
    this.lists.reload();
    void this.ctx.load(true);
  }
}
