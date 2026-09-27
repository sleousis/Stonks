import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
  signal,
} from '@angular/core';

import type { TelegramLinkCodeView } from '../../api/models';
import { TelegramService } from '../../api/telegram.service';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDate, formatTime } from '../../core/format/format';
import { ToastService } from '../../core/notify/toast.service';
import { OneTimeSecret } from '../../shared/ui/one-time-secret';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import { StatusPill } from '../../shared/ui/status-pill';

/** A t.me link to the bot, or null when the server does not know its name. */
export function botLink(username: string | null | undefined): string | null {
  const name = username?.replace(/^@/, '').trim();
  return name && /^[A-Za-z0-9_]{3,64}$/.test(name) ? `https://t.me/${name}` : null;
}

/**
 * Settings, Telegram: whether your chat is linked, a one-time code to link
 * it (shown once, sent to the bot as `/link CODE`), and unlinking. A linked
 * chat gets your notifications and can ask the bot for status, positions and
 * signals, and stop trading.
 */
@Component({
  selector: 'app-telegram-link',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OneTimeSecret, PermissionNote, StatusPill, LoadingState, ErrorState],
  template: `
    <section class="panel" aria-labelledby="telegram-title">
      <div class="panel-head">
        <h3 id="telegram-title">Telegram</h3>
        @if (link.hasValue()) {
          <app-status-pill
            [status]="link.value().linked ? 'active' : 'idle'"
            [label]="link.value().linked ? 'Linked' : 'Not linked'"
            [tone]="link.value().linked ? 'positive' : 'neutral'"
          />
        }
      </div>
      <div class="panel-body body">
        @if (link.error(); as err) {
          <app-error-state
            title="Could not read your Telegram link"
            [error]="err"
            (retry)="link.reload()"
          />
        } @else if (!link.hasValue()) {
          <app-loading-state label="Loading your Telegram link" [rows]="2" />
        } @else {
          @let l = link.value();
          @if (!l.bot_configured) {
            <p class="lead">
              This server has no Telegram bot yet, so nothing can be sent there. An admin can add
              one.
            </p>
          } @else if (l.linked) {
            <p class="lead">
              Your notifications also go to
              <strong>{{ l.username ? '@' + l.username : 'your Telegram chat' }}</strong
              >{{ l.linked_at ? ', linked on ' + day(l.linked_at) : '' }}.
            </p>
            @if (l.bot_enabled) {
              <p class="hint">
                In the chat, ask for status, today, positions or signals, or stop trading. Resuming
                trading works only here in the console.
              </p>
            }
            <div class="actions">
              <button
                type="button"
                class="btn btn-danger"
                [disabled]="busy() || !canManage()"
                (click)="unlink()"
              >
                Unlink Telegram
              </button>
              <app-permission-note permission="notifications.manage" />
            </div>
          } @else {
            <p class="lead">
              Link your Telegram chat to get your notifications there. Make a code, then send it to
              the bot.
            </p>
            @if (code(); as c) {
              <app-one-time-secret
                [values]="['/link ' + c.code]"
                label="Link message"
                [warning]="'Shown once. Send this to the bot before ' + time(c.expires_at) + '.'"
              />
              <ol class="steps">
                <li>
                  Open
                  @if (bot(); as href) {
                    <a [href]="href" target="_blank" rel="noopener noreferrer">{{
                      '@' + botName()
                    }}</a>
                  } @else {
                    the bot
                  }
                  in Telegram.
                </li>
                <li>Send the message above in a private chat.</li>
                <li>Come back and check the link.</li>
              </ol>
              <div class="actions">
                <button type="button" class="btn btn-primary" (click)="checkLink()">
                  Check the link
                </button>
                <button type="button" class="btn" [disabled]="busy()" (click)="makeCode()">
                  New code
                </button>
              </div>
            } @else {
              <div class="actions">
                <button
                  type="button"
                  class="btn btn-primary"
                  [disabled]="busy() || !canManage()"
                  (click)="makeCode()"
                >
                  Get a link code
                </button>
                <app-permission-note permission="notifications.manage" />
              </div>
            }
            @if (!l.bot_enabled) {
              <p class="hint">The bot sends notifications here, but does not answer messages.</p>
            }
          }
        }
      </div>
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    .body {
      display: grid;
      gap: var(--space-3);
    }
    .lead {
      color: var(--color-ink-2);
      overflow-wrap: anywhere;
    }
    .steps {
      display: grid;
      gap: var(--space-1);
      margin: 0;
      padding-left: var(--space-5);
      font-size: var(--text-sm);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
    }
  `,
})
export class TelegramLink {
  private readonly api = inject(TelegramService);
  private readonly session = inject(SessionService);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);

  protected readonly link = resource({ loader: () => this.api.status() });
  /** The code just made. Kept only while this panel lives. */
  protected readonly code = signal<TelegramLinkCodeView | null>(null);
  protected readonly busy = signal(false);
  protected readonly canManage = computed(() => this.session.can('notifications.manage'));

  protected readonly botName = computed(() => {
    const fromCode = this.code()?.bot_username;
    const fromLink = this.link.hasValue() ? this.link.value().bot_username : null;
    return (fromCode ?? fromLink ?? '').replace(/^@/, '');
  });
  protected readonly bot = computed(() => botLink(this.botName()));

  protected day(v: string): string {
    return formatDate(v);
  }
  protected time(v: string): string {
    return formatTime(v);
  }

  async makeCode(): Promise<void> {
    this.busy.set(true);
    try {
      this.code.set(await this.api.createCode());
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }

  protected checkLink(): void {
    this.link.reload();
  }

  async unlink(): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Unlink Telegram?',
      message: 'Your notifications stop going to Telegram, and the bot stops answering you.',
      confirmLabel: 'Unlink',
      tone: 'danger',
    });
    if (!ok) return;
    this.busy.set(true);
    try {
      await this.api.unlink();
      this.code.set(null);
      this.toasts.success('Unlinked Telegram.');
      this.link.reload();
    } catch {
      // The error interceptor already showed the API's message.
    } finally {
      this.busy.set(false);
    }
  }
}
