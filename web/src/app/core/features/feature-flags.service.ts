import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { AssistantService } from '../../api/assistant.service';
import { ModelVersionsService } from '../../api/model-versions.service';
import { OptionsService } from '../../api/options.service';
import { StreamService } from '../../api/stream.service';
import { SessionService } from '../auth/session.service';

/**
 * Features that are off out of the box, so the nav hides them until they are on:
 * - `assistant`: the server has a model to talk to;
 * - `options`: option chains are stored (options research, Phase 17);
 * - `intraday`: the intraday engine streams or has run (admins);
 * - `models`: a strategy that learns from data has a new fit waiting (admins).
 */
export type Feature = 'assistant' | 'options' | 'intraday' | 'models';

/**
 * Which optional features this server has on (F42, AO-11). Read once after
 * sign-in. Until the answer arrives, and when a read fails, a feature
 * counts as on: hiding a page someone uses is worse than showing one that
 * says it is off.
 */
@Injectable({ providedIn: 'root' })
export class FeatureFlagsService {
  private readonly session = inject(SessionService);
  private readonly assistant = inject(AssistantService);
  private readonly options = inject(OptionsService);
  private readonly stream = inject(StreamService);
  private readonly models = inject(ModelVersionsService);

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
    const admin = untracked(() => this.session.isAdmin());
    await Promise.all([
      this.read('assistant', async () => (await this.assistant.status()).enabled),
      this.read('options', async () => (await this.options.underlyings()).length > 0),
      admin
        ? this.read('intraday', async () => {
            const s = await this.stream.status();
            return s.streaming_enabled || s.engines.length > 0;
          })
        : null,
      admin ? this.read('models', async () => (await this.models.candidates()).length > 0) : null,
    ]);
  }

  private async read(feature: Feature, check: () => Promise<boolean>): Promise<void> {
    try {
      const on = await check();
      this.flags.update((f) => ({ ...f, [feature]: on }));
    } catch {
      // Unknown: keep the item.
    }
  }
}
