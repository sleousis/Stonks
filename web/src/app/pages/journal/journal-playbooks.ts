import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import { JournalService } from '../../api/journal.service';
import type { PlaybookView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ToastService } from '../../core/notify/toast.service';
import { PermissionNote } from '../../shared/ui/permission-note';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/states';

/**
 * Your playbooks: the setups you trade and the rules you mean to follow.
 * Pick one on a trade when you review it, then compare results by playbook.
 */
@Component({
  selector: 'app-journal-playbooks',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PermissionNote, EmptyState, ErrorState, LoadingState],
  styleUrl: './journal.scss',
  template: `
    <section class="panel" aria-labelledby="pb-title">
      <div class="panel-head">
        <h2 id="pb-title">Playbooks</h2>
      </div>
      <p class="lead">
        A playbook is a setup you trade, such as a breakout or an earnings gap, with the rules you
        mean to follow. Archived playbooks keep their trades.
      </p>

      @if (playbooks.error(); as err) {
        <app-error-state
          title="Could not load your playbooks"
          [error]="err"
          (retry)="playbooks.reload()"
        />
      } @else if (!playbooks.hasValue()) {
        <app-loading-state label="Loading your playbooks" [rows]="3" />
      } @else if (playbooks.value().length === 0) {
        <app-empty-state
          title="No playbooks yet"
          message="Add the setups you trade, then pick one when you review a trade."
        />
      } @else {
        <ul class="playbooks">
          @for (p of playbooks.value(); track p.id) {
            <li [class.archived]="p.archived">
              <div>
                <p class="pb-name">
                  {{ p.name }}
                  @if (p.archived) {
                    <span class="muted">(archived)</span>
                  }
                </p>
                @if (p.description) {
                  <p class="muted pb-rules">{{ p.description }}</p>
                }
              </div>
              @if (canWrite()) {
                <button
                  type="button"
                  class="btn btn-ghost"
                  [disabled]="busy()"
                  (click)="toggle(p)"
                  [attr.aria-label]="(p.archived ? 'Restore ' : 'Archive ') + p.name"
                >
                  {{ p.archived ? 'Restore' : 'Archive' }}
                </button>
              }
            </li>
          }
        </ul>
      }

      @if (canWrite()) {
        <form class="pb-form" (submit)="$event.preventDefault(); add()">
          <div class="field">
            <label for="pb-name">Name</label>
            <input
              id="pb-name"
              class="input"
              maxlength="60"
              [value]="name()"
              (input)="name.set($any($event.target).value)"
            />
          </div>
          <div class="field">
            <label for="pb-rules">Rules</label>
            <textarea
              id="pb-rules"
              class="input"
              rows="3"
              maxlength="2000"
              [value]="rules()"
              (input)="rules.set($any($event.target).value)"
            ></textarea>
          </div>
          <button type="submit" class="btn btn-primary" [disabled]="busy() || !name().trim()">
            Add playbook
          </button>
        </form>
      } @else {
        <app-permission-note permission="portfolio.manage" />
      }
    </section>
  `,
})
export class JournalPlaybooks {
  private readonly journal = inject(JournalService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);

  protected readonly canWrite = computed(() => this.session.can('portfolio.manage'));
  protected readonly name = signal('');
  protected readonly rules = signal('');
  protected readonly busy = signal(false);

  protected readonly playbooks = resource({
    loader: () => this.journal.playbooks(true),
  });

  protected async add(): Promise<void> {
    const name = this.name().trim();
    if (!name) return;
    this.busy.set(true);
    try {
      await this.journal.createPlaybook({ name, description: this.rules().trim() || null });
      this.name.set('');
      this.rules.set('');
      this.toasts.success(`Added the playbook ${name}.`);
      this.playbooks.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  protected async toggle(p: PlaybookView): Promise<void> {
    this.busy.set(true);
    try {
      await this.journal.updatePlaybook(p.id, { archived: !p.archived });
      this.playbooks.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
