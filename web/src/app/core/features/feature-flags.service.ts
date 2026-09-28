import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { AssistantService } from '../../api/assistant.service';
import { SessionService } from '../auth/session.service';

/** Features that are off out of the box, so the nav hides them until they are on. */
export type Feature = 'assistant';

/**
 * Which optional features this server has on (F42). Read once after
 * sign-in. Until the answer arrives, and when the read fails, a feature
 * counts as on: hiding a page someone uses is worse than showing one that
 * says it is off.
 */
@Injectable({ providedIn: 'root' })
export class FeatureFlagsService {
  private readonly session = inject(SessionService);
  private readonly assistant = inject(AssistantService);

  private readonly flags = signal<Partial<Record<Feature, boolean>>>({});
  private loadedFor: string | null = null;

  constructor() {
    effect(() => {
      const id = this.session.me()?.user_id ?? null;
      untracked(() => {
        if (!id) {
          this.loadedFor = null;
          this.flags.set({});
          return;
        }
        if (id === this.loadedFor) return;
        this.loadedFor = id;
        void this.load();
      });
    });
  }

  /** True unless the server said the feature is off. Reactive. */
  on(feature: Feature): boolean {
    return this.flags()[feature] !== false;
  }

  /** The server said so: the assistant has no model to talk to. */
  readonly assistantOff = computed(() => this.flags().assistant === false);

  private async load(): Promise<void> {
    try {
      const status = await this.assistant.status();
      this.flags.update((f) => ({ ...f, assistant: status.enabled }));
    } catch {
      // Unknown: keep the item.
    }
  }
}
