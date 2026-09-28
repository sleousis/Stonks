import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  resource,
  signal,
  untracked,
} from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { type AssistantEvent, AssistantService } from '../../api/assistant.service';
import type { ConversationView, TurnView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { formatDateTime } from '../../core/format/format';
import { errorMessage } from '../../core/http/api-error';
import { ToastService } from '../../core/notify/toast.service';
import { PageHeader } from '../../shared/ui/page-header';
import { PermissionNote } from '../../shared/ui/permission-note';
import { ErrorState, LoadingState } from '../../shared/ui/states';
import {
  type ChatItem,
  applyEvent,
  decideItem,
  fromHistory,
  pendingConfirm,
  userItem,
} from './chat-model';
import { ChatStep } from './chat-step';
import { ConfirmStep } from './confirm-step';
import { TraceSheet } from './trace-sheet';

const MESSAGE_MAX = 8000;

/** Is the freeze still on at `now`? */
export function frozenUntil(until: string | null | undefined, now = Date.now()): string | null {
  if (!until) return null;
  const at = Date.parse(until);
  return Number.isFinite(at) && at > now ? until : null;
}

/**
 * The AI assistant: a chat over your own model server that reads and acts
 * through the same tools as the rest of Stonks, as you. Answers stream in,
 * each tool it uses shows as a step, and anything that would change
 * something waits for your yes. Conversations are listed on the left (a
 * list of their own on phones), and the trace shows how each answer came
 * about. `?c=<id>` opens a conversation.
 */
@Component({
  selector: 'app-assistant-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    PageHeader,
    PermissionNote,
    LoadingState,

    ErrorState,
    ChatStep,
    ConfirmStep,
    TraceSheet,
  ],
  templateUrl: './assistant.page.html',
  styleUrl: './assistant.page.scss',
})
export class AssistantPage {
  private readonly api = inject(AssistantService);
  private readonly router = inject(Router);
  private readonly confirm = inject(ConfirmService);
  private readonly toasts = inject(ToastService);
  private readonly session = inject(SessionService);

  /** The open conversation, from `?c=`. */
  readonly c = input<string | undefined>();

  protected readonly messageMax = MESSAGE_MAX;
  protected readonly status = resource({ loader: () => this.api.status() });
  protected readonly list = resource({
    params: () => (this.status.value()?.enabled ? {} : undefined),
    loader: () => this.api.conversations(),
  });

  /** The conversation on screen; null for a new one. */
  protected readonly currentId = signal<string | null>(null);
  protected readonly items = signal<ChatItem[]>([]);
  protected readonly historyState = signal<'idle' | 'loading' | 'ready' | 'failed'>('idle');
  protected readonly historyError = signal<unknown>(null);
  protected readonly current = computed<ConversationView | null>(() => {
    const id = this.currentId();
    if (!id) return null;
    return (this.list.value() ?? []).find((c) => c.id === id) ?? this.opened();
  });
  private readonly opened = signal<ConversationView | null>(null);

  protected readonly draft = signal('');
  protected readonly researchOnly = signal(false);
  protected readonly streaming = signal(false);
  protected readonly deciding = signal(false);
  protected readonly announce = signal('');
  private controller: AbortController | null = null;

  protected readonly pending = computed(() => pendingConfirm(this.items()));
  protected readonly canSend = computed(
    () =>
      !this.streaming() &&
      !this.deciding() &&
      this.draft().trim().length > 0 &&
      this.draft().length <= MESSAGE_MAX,
  );

  protected readonly frozen = computed(() => frozenUntil(this.status.value()?.frozen_until));
  protected readonly frozenText = computed(() => {
    const until = this.frozen();
    return until ? formatDateTime(until) : '';
  });
  protected readonly canUnfreeze = computed(() => this.session.can('killswitch.resume'));
  protected readonly unfreezing = signal(false);

  protected readonly traceOpen = signal(false);
  protected readonly traceTurns = signal<TurnView[] | null>(null);
  protected readonly traceError = signal<unknown>(null);

  protected readonly title = computed(() => {
    const c = this.current();
    if (!this.currentId()) return 'New conversation';
    return c?.title?.trim() || 'Untitled conversation';
  });
  protected readonly when = (c: ConversationView) => formatDateTime(c.updated_at);

  constructor() {
    effect(() => {
      const id = this.c() ?? null;
      untracked(() => void this.open(id));
    });
    inject(DestroyRef).onDestroy(() => this.controller?.abort());
  }

  /** Show a conversation (or a blank new one). Skips the one already on screen. */
  private async open(id: string | null): Promise<void> {
    if (id === this.currentId() && this.historyState() !== 'failed') return;
    this.controller?.abort();
    this.currentId.set(id);
    this.traceOpen.set(false);
    this.traceTurns.set(null);
    if (!id) {
      this.items.set([]);
      this.opened.set(null);
      this.historyState.set('idle');
      return;
    }
    await this.loadHistory(id);
  }

  protected async loadHistory(id = this.currentId()): Promise<void> {
    if (!id) return;
    this.historyState.set('loading');
    this.historyError.set(null);
    try {
      const detail = await this.api.conversation(id);
      if (this.currentId() !== id) return;
      this.opened.set(detail);
      this.items.set(fromHistory(detail));
      this.historyState.set('ready');
    } catch (err) {
      if (this.currentId() !== id) return;
      this.historyError.set(err);
      this.historyState.set('failed');
    }
  }

  protected newConversation(): void {
    this.draft.set('');
    void this.router.navigate(['/assistant']);
  }

  protected async send(): Promise<void> {
    if (!this.canSend()) return;
    const text = this.draft().trim();
    let id = this.currentId();
    this.streaming.set(true);
    try {
      if (!id) {
        const made = await this.api.create(undefined, this.researchOnly());
        id = made.id;
        this.opened.set(made);
        this.currentId.set(id);
        this.historyState.set('ready');
        void this.router.navigate(['/assistant'], { queryParams: { c: id } });
      }
      this.draft.set('');
      this.items.update((items) => [...items, userItem(text)]);
      await this.follow((signal) => this.api.send(id!, text, signal));
    } catch {
      // Creating failed: the error interceptor showed the API's message.
      this.streaming.set(false);
    }
  }

  protected async decide(actionId: string, approve: boolean): Promise<void> {
    const id = this.currentId();
    if (!id || this.deciding() || this.streaming()) return;
    this.deciding.set(true);
    this.items.update((items) => decideItem(items, actionId, approve));
    try {
      await this.follow((signal) => this.api.decide(id, actionId, approve, signal));
    } finally {
      this.deciding.set(false);
    }
  }

  /** Stop following the answer (the server stops the turn when the stream closes). */
  protected stop(): void {
    this.controller?.abort();
  }

  private async follow(
    open: (signal: AbortSignal) => AsyncGenerator<AssistantEvent>,
  ): Promise<void> {
    const id = this.currentId();
    const controller = new AbortController();
    this.controller = controller;
    this.streaming.set(true);
    this.announce.set('The assistant is answering.');
    let waiting = false;
    try {
      for await (const event of open(controller.signal)) {
        if (this.currentId() !== id) break;
        this.items.update((items) => applyEvent(items, event));
        if (event.kind === 'done') waiting = !!event.data['pending_action_id'];
      }
      this.announce.set(
        controller.signal.aborted
          ? 'Stopped.'
          : waiting
            ? 'The assistant needs your yes to go on.'
            : 'The answer is complete.',
      );
    } catch (err) {
      if (this.currentId() === id && !controller.signal.aborted) {
        this.items.update((items) =>
          applyEvent(items, {
            kind: 'error',
            data: { code: 'request', message: errorMessage(err) },
          }),
        );
        this.announce.set('The assistant could not answer.');
      }
    } finally {
      if (this.controller === controller) this.controller = null;
      this.items.update((items) => applyEvent(items, { kind: 'done', data: {} }));
      this.streaming.set(false);
      this.list.reload();
      this.status.reload();
    }
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void this.send();
    }
  }

  protected async remove(c: ConversationView): Promise<void> {
    const ok = await this.confirm.confirm({
      title: `Delete "${c.title || 'Untitled conversation'}"?`,
      message: 'The messages and their trace go for good.',
      confirmLabel: 'Delete conversation',
      tone: 'danger',
    });
    if (!ok) return;
    try {
      await this.api.delete(c.id);
      this.toasts.success('Deleted the conversation.');
      if (this.currentId() === c.id) this.newConversation();
      this.list.reload();
    } catch {
      // The error interceptor already showed the API's message.
    }
  }

  protected async openTrace(): Promise<void> {
    const id = this.currentId();
    if (!id) return;
    this.traceOpen.set(true);
    this.traceTurns.set(null);
    this.traceError.set(null);
    try {
      const turns = await this.api.turns(id);
      if (this.currentId() === id) this.traceTurns.set(turns);
    } catch (err) {
      this.traceError.set(err);
    }
  }

  protected async unfreeze(): Promise<void> {
    const ok = await this.confirm.confirm({
      title: 'Unfreeze the assistant?',
      message:
        'It froze itself after many changes in a short time. Unfreeze it only if you asked for those changes.',
      confirmLabel: 'Unfreeze',
    });
    if (!ok) return;
    this.unfreezing.set(true);
    try {
      await this.api.clearFreeze();
      this.toasts.success('Unfroze the assistant.');
      this.status.reload();
    } catch {
      // The error interceptor (or the cancelled step-up) already said why.
    } finally {
      this.unfreezing.set(false);
    }
  }
}
