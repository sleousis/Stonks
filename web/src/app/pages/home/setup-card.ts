import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { OnboardingService } from '../../api/onboarding.service';
import { SessionService } from '../../core/auth/session.service';
import { STEP_COPY, progressText } from '../welcome/welcome-steps';

/**
 * "Finish setting up" on Today, until every first-run step is done or
 * skipped, or the trader closes the guide. Silent: when the guide cannot
 * load, Today simply does not show it.
 */
@Component({
  selector: 'app-setup-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (view(); as v) {
      <section class="setup" aria-labelledby="setup-title">
        <div class="text">
          <h2 id="setup-title">Finish setting up</h2>
          <p class="line">
            <span class="num">{{ progress() }}</span
            >. Next: {{ next() }}.
          </p>
          <ol class="ticks" aria-hidden="true">
            @for (s of v.steps; track s.id) {
              <li [attr.data-state]="s.state"></li>
            }
          </ol>
        </div>
        <div class="actions">
          <a routerLink="/welcome" class="btn btn-primary">Continue setup</a>
          <button type="button" class="btn btn-ghost" [disabled]="hiding()" (click)="hide()">
            Hide
          </button>
        </div>
      </section>
    }
  `,
  styles: `
    :host {
      display: block;
    }
    .setup {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-3) var(--space-4);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--color-border);
      border-left: 3px solid var(--color-accent);
      border-radius: var(--radius-md);
      background: var(--color-surface);
      box-shadow: var(--shadow-1);
    }
    .text {
      display: grid;
      gap: var(--space-1);
      min-width: 0;
      flex: 1 1 16rem;
    }
    h2 {
      font-family: var(--font-display);
      font-stretch: var(--display-stretch);
      font-size: var(--text-lg);
      font-weight: var(--weight-bold);
    }
    .line {
      color: var(--color-ink-2);
      font-size: var(--text-sm);
    }
    .ticks {
      display: flex;
      gap: 4px;
      margin: var(--space-1) 0 0;
      padding: 0;
      list-style: none;
    }
    .ticks li {
      width: 2rem;
      height: 4px;
      border-radius: 2px;
      background: var(--color-border);
    }
    .ticks li[data-state='done'] {
      background: var(--color-accent);
    }
    .ticks li[data-state='skipped'] {
      background: var(--color-border-strong);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
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

  protected readonly view = computed(() => {
    const v = this.guide.hasValue() ? this.guide.value() : null;
    return v && v.show ? v : null;
  });
  protected readonly progress = computed(() => {
    const v = this.view();
    return v ? progressText(v.steps) : '';
  });
  protected readonly next = computed(() => {
    const step = this.view()?.steps.find((s) => s.state === 'todo');
    return step ? STEP_COPY[step.id].title.toLowerCase() : '';
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
