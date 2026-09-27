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

/** How often the app re-reads active halts for the banner. */
export const HALT_POLL_MS = new InjectionToken<number>('HALT_POLL_MS', {
  providedIn: 'root',
  factory: () => 60_000,
});

/** "Global", "Portfolio pf_default", "User usr_owner". */
export function haltScopeText(h: Pick<HaltView, 'scope' | 'portfolio_id' | 'user_id'>): string {
  if (h.scope === 'portfolio') return `Portfolio ${h.portfolio_id ?? ''}`.trim();
  if (h.scope === 'user') return `User ${h.user_id ?? ''}`.trim();
  return 'Global';
}

/**
 * Active halts, shared by the halts page and the app-wide banner. Polls
 * quietly (no error toasts) and refreshes right after any halt action.
 */
@Injectable({ providedIn: 'root' })
export class HaltStateService {
  private readonly api = inject(HaltsService);
  private readonly pollMs = inject(HALT_POLL_MS);
  private readonly session = inject(SessionService);
  private readonly injector = inject(Injector);

  private readonly halts = signal<readonly HaltView[]>([]);
  private timer: ReturnType<typeof setInterval> | null = null;

  /** Every active halt from the last read. */
  readonly active = this.halts.asReadonly();
  /** Active kill switches, the ones the banner warns about. */
  readonly kills = computed(() => this.halts().filter((h) => h.kind === 'kill' && h.active));

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
    this.timer = setInterval(() => void this.refresh(), this.pollMs);
    destroyRef.onDestroy(() => {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
    });
  }

  /** Re-read active halts. A failed read keeps the last known state. */
  async refresh(): Promise<void> {
    if (!this.session.canRead()) return;
    try {
      this.halts.set(await this.api.list(false, true));
    } catch {
      // Offline or signed out: keep what we had rather than hiding a live kill switch.
    }
  }
}
