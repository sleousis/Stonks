import {
  DestroyRef,
  Injectable,
  InjectionToken,
  Injector,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';

import { HaltsService } from '../../api/halts.service';
import type { HaltView } from '../../api/models';
import { SessionService } from '../auth/session.service';
import { PortfolioContextService } from '../portfolio/portfolio-context.service';
import { type HaltScope, haltScopeText, haltSummary } from './halt-view';

export { haltScopeText } from './halt-view';

/** How often the app re-reads active halts for the banner. */
export const HALT_POLL_MS = new InjectionToken<number>('HALT_POLL_MS', {
  providedIn: 'root',
  factory: () => 60_000,
});

/**
 * Active halts, shared by the halts page, the session strip and the Stop
 * trading sheet. Polls quietly (no error toasts) and refreshes right after
 * any halt action.
 */
@Injectable({ providedIn: 'root' })
export class HaltStateService {
  private readonly api = inject(HaltsService);
  private readonly pollMs = inject(HALT_POLL_MS);
  private readonly session = inject(SessionService);
  private readonly portfolios = inject(PortfolioContextService);
  private readonly injector = inject(Injector);

  private readonly halts = signal<readonly HaltView[]>([]);
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Numbers each read; only the newest one may write (UX-52). */
  private sequence = 0;
  /** Reads in flight; a poll never stacks on top of one. */
  private reading = 0;

  /** Every active halt from the last read. */
  readonly active = this.halts.asReadonly();
  /** Active kill switches, the ones the banner warns about. */
  readonly kills = computed(() => this.halts().filter((h) => h.kind === 'kill' && h.active));
  /** A kill switch is on somewhere the user can see. */
  readonly killOn = computed(() => this.kills().length > 0);

  /** Who a halt covers, with portfolio names and "Your portfolios" (UX-17). */
  readonly scopeText = computed(() => {
    const names = new Map(this.portfolios.options().map((p) => [p.id, p.name] as const));
    const meId = this.session.me()?.user_id ?? null;
    return (h: HaltScope) => haltScopeText(h, names, meId);
  });

  /** The banner's words for the active halts, or null. */
  readonly summary = computed(() => haltSummary(this.halts(), this.scopeText()));

  /**
   * Start polling until `destroyRef` goes (the shell's lifetime). Reads
   * start once someone may read (signed in, or open reads), never before.
   */
  watch(destroyRef: DestroyRef): void {
    const ref = effect(
      () => {
        if (this.session.canRead()) untracked(() => void this.refresh());
      },
      { injector: this.injector },
    );
    destroyRef.onDestroy(() => ref.destroy());
    if (this.timer || this.pollMs <= 0) return;
    this.timer = setInterval(() => {
      if (this.reading === 0) void this.refresh();
    }, this.pollMs);
    destroyRef.onDestroy(() => {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
    });
  }

  /**
   * Show a halt the user just turned on at once (the strip turns red before
   * the next read), and drop any read already in flight, which predates it.
   */
  add(halt: HaltView): void {
    this.sequence++;
    this.halts.update((list) => [...list.filter((h) => h.id !== halt.id), halt]);
  }

  /**
   * Re-read active halts. A failed read keeps the last known state, and an
   * answer that arrives after a newer read started is dropped, so a slow
   * old read never brings back a kill switch that was just resumed.
   */
  async refresh(): Promise<void> {
    if (!this.session.canRead()) return;
    const mine = ++this.sequence;
    this.reading++;
    try {
      const list = await this.api.list(false, true);
      if (mine === this.sequence) this.halts.set(list);
    } catch {
      // Offline or signed out: keep what we had rather than hiding a live kill switch.
    } finally {
      this.reading--;
    }
  }
}
