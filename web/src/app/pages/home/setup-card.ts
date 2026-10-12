import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { OnboardingService } from '../../api/onboarding.service';
import { SessionService } from '../../core/auth/session.service';
import { STEP_COPY, currentStep, progressText, showSetup } from '../welcome/welcome-steps';

/**
 * "Get set up" on Today, until every first-run step that matters is done
 * or skipped (the portfolio and watchlist steps are optional), or the
 * trader closes the guide. Silent: when the guide cannot
 * load, Today simply does not show it.
 */
@Component({
  selector: 'app-setup-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (reserve()) {
      <!-- While the guide loads, hold its place so Today does not jump. -->
      <div class="setup band reserve" aria-hidden="true">
        <div class="text">
          <span class="title">&nbsp;</span>
          <span class="line">&nbsp;</span>
        </div>
        <div class="actions"><span class="btn">&nbsp;</span></div>
      </div>
    } @else if (view(); as v) {
      <section class="setup band" aria-labelledby="setup-title">
        <div class="text">
          <div class="title-row">
            <h2 id="setup-title" class="title">Get set up</h2>
            <span class="num progress">{{ progress() }}</span>
          </div>
          <p class="line">Next: {{ next() }}.</p>
        </div>
        <ol class="ticks" aria-hidden="true">
          @for (s of counted(); track s.id) {
            <li [attr.data-state]="s.state"></li>
          }
        </ol>
        <div class="actions">
          <a routerLink="/welcome" class="btn btn-primary">Continue setup</a>
          <a routerLink="/demo" class="btn">See a demo</a>
          <button type="button" class="btn btn-ghost" [disabled]="hiding()" (click)="hide()">
            Hide
          </button>
        </div>
      </section>
    }
  `,
  styles: `
    @use 'breakpoints' as bp;
    :host {
      display: block;
    }
    /* The setup band: text, a progress line and the actions in one row. */
    .setup {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-3) var(--space-6);
    }
    .text {
      display: grid;
      gap: 2px;
      min-width: 0;
      flex: 1 1 16rem;
    }
    .title-row {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--space-2);
    }
    .title {
      font-size: var(--text-body);
      font-weight: var(--weight-semibold);
    }
    .progress {
      color: var(--muted);
      font-size: var(--text-sm);
    }
    .line {
      color: var(--ink-2);
      font-size: var(--text-md);
    }
    .ticks {
      display: flex;
      gap: var(--space-1);
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .ticks li {
      width: 28px;
      height: 4px;
      border-radius: 2px;
      background: var(--accent-line);
    }
    .ticks li[data-state='done'] {
      background: var(--accent);
    }
    .ticks li[data-state='skipped'] {
      background: var(--rule-control);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    /* Phones: stacked, Continue setup across the row, the other two share the next. */
    @include bp.phone {
      .setup {
        display: grid;
        gap: var(--space-3);
      }
      .actions {
        display: grid;
        grid-template-columns: 1fr 1fr;
        width: 100%;
      }
      .actions .btn-primary {
        grid-column: 1 / -1;
      }
    }
    .reserve {
      visibility: hidden;
    }
    .reserve .title,
    .reserve .line {
      display: block;
    }
  `,
})
export class SetupCard {
  private readonly api = inject(OnboardingService);
  private readonly session = inject(SessionService);
  /** Only for someone signed in: the guide is per person. */
  private readonly guide = resource({
    params: () => (this.session.signedIn() ? { signedIn: true } : undefined),
    loader: async () => {
      try {
        return await this.api.get(true);
      } catch {
        return null;
      }
    },
  });
  protected readonly hiding = signal(false);
  /** The guide showed last time on this device (per person), so hold its place. */
  private readonly lastShown = computed(() => readShown(this.session.me()?.user_id));
  protected readonly reserve = computed(
    () => this.guide.isLoading() && !this.guide.hasValue() && this.lastShown(),
  );

  constructor() {
    effect(() => {
      const id = this.session.me()?.user_id;
      if (!id || !this.guide.hasValue()) return;
      const v = this.guide.value();
      writeShown(id, !!v && showSetup(v));
    });
  }

  protected readonly view = computed(() => {
    const v = this.guide.hasValue() ? this.guide.value() : null;
    return v && showSetup(v) ? v : null;
  });
  /** The steps that matter: the ticks count the same ones as the text. */
  protected readonly counted = computed(() => this.view()?.steps.filter((s) => !s.optional) ?? []);
  protected readonly progress = computed(() => {
    const v = this.view();
    return v ? progressText(v.steps) : '';
  });
  protected readonly next = computed(() => {
    const v = this.view();
    const step = v ? currentStep(v) : null;
    return step ? STEP_COPY[step].title.toLowerCase() : '';
  });

  protected async hide(): Promise<void> {
    this.hiding.set(true);
    try {
      this.guide.set(await this.api.setDismissed(true));
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.hiding.set(false);
    }
  }
}

const SHOWN_KEY = 'stonks.setupShown.';

/** Unknown counts as shown: a new trader sees the guide. */
function readShown(userId: string | undefined): boolean {
  if (!userId) return false;
  try {
    return localStorage.getItem(SHOWN_KEY + userId) !== '0';
  } catch {
    return true;
  }
}

function writeShown(userId: string, shown: boolean): void {
  try {
    localStorage.setItem(SHOWN_KEY + userId, shown ? '1' : '0');
  } catch {
    // Storage blocked: Today may shift once when the guide loads.
  }
}
