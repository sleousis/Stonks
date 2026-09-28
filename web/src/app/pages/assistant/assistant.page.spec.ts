import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { type AssistantEvent, AssistantService } from '../../api/assistant.service';
import type {
  AssistantStatusView,
  ConversationDetailView,
  ConversationView,
} from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ConfirmService } from '../../core/confirm/confirm.service';
import { ToastService } from '../../core/notify/toast.service';
import { tick } from '../../../testing/http';
import { AssistantPage, frozenUntil } from './assistant.page';

const ON: AssistantStatusView = {
  enabled: true,
  model: 'llama3.1',
  max_steps: 8,
  max_tokens: 1024,
  timeout_seconds: 120,
  prompt_version: 'v1',
  order_tools: false,
  research_only: true,
  frozen_until: null,
  reason: null,
};

const CONV: ConversationView = {
  id: 'c1',
  title: 'How am I doing?',
  research_only: false,
  created_at: '2026-09-20T10:00:00Z',
  updated_at: '2026-09-20T10:05:00Z',
};

const DETAIL: ConversationDetailView = {
  ...CONV,
  messages: [
    { id: 1, role: 'user', content: 'How am I doing?', created_at: '' },
    { id: 2, role: 'assistant', content: 'Up 2% this month.', created_at: '' },
  ],
  pending_actions: [],
};

async function* stream(events: AssistantEvent[]): AsyncGenerator<AssistantEvent> {
  for (const e of events) {
    await tick();
    yield e;
  }
}

describe('AssistantPage', () => {
  let fixture: ComponentFixture<AssistantPage>;
  let el: HTMLElement;
  let status: AssistantStatusView;
  let api: {
    status: ReturnType<typeof vi.fn>;
    conversations: ReturnType<typeof vi.fn>;
    conversation: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
    send: ReturnType<typeof vi.fn>;
    decide: ReturnType<typeof vi.fn>;
    turns: ReturnType<typeof vi.fn>;
    delete: ReturnType<typeof vi.fn>;
    clearFreeze: ReturnType<typeof vi.fn>;
  };
  let confirm: ReturnType<typeof vi.fn>;
  let allowed: boolean;
  let admin: boolean;

  beforeEach(() => {
    status = ON;
    allowed = true;
    admin = false;
    confirm = vi.fn().mockResolvedValue(true);
    api = {
      status: vi.fn(async () => status),
      conversations: vi.fn(async () => [CONV]),
      conversation: vi.fn(async () => DETAIL),
      create: vi.fn(async (_t: string | undefined, research: boolean) => ({
        ...CONV,
        id: 'c2',
        title: '',
        research_only: research,
      })),
      send: vi.fn(),
      decide: vi.fn(),
      turns: vi.fn(async () => []),
      delete: vi.fn(async () => undefined),
      clearFreeze: vi.fn(async () => undefined),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'assistant', children: [] }]),
        { provide: AssistantService, useValue: api },
        { provide: ConfirmService, useValue: { confirm } },
        {
          provide: SessionService,
          useValue: { can: () => allowed, whyNot: () => null, isAdmin: () => admin },
        },
      ],
    });
  });

  async function render(c?: string): Promise<void> {
    fixture = TestBed.createComponent(AssistantPage);
    if (c) fixture.componentRef.setInput('c', c);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await settle();
  }

  async function settle(times = 4): Promise<void> {
    for (let i = 0; i < times; i++) {
      await tick();
      fixture.detectChanges();
    }
  }

  function type(text: string): void {
    const area = el.querySelector<HTMLTextAreaElement>('#assistant-message')!;
    area.value = text;
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function submit(): void {
    el.querySelector<HTMLFormElement>('form.composer')!.dispatchEvent(new Event('submit'));
  }

  function button(text: string): HTMLButtonElement {
    return [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === text)!;
  }

  it('says clearly when the assistant is off, with no chat', async () => {
    status = { ...ON, enabled: false, model: null };
    await render();
    // A calm state, not an error: what it needs, who sets it up, and a way on.
    expect(el.querySelector('h2')?.textContent).toContain('not set up on this server');
    expect(el.textContent).toContain('Nothing is wrong');
    expect(el.querySelector('app-error-state')).toBeNull();
    expect(el.querySelector('a[href="/"]')?.textContent?.trim()).toBe('Back to Today');
    expect(button('New conversation')).toBeUndefined();
    expect(el.querySelector('#assistant-message')).toBeNull();
    expect(api.conversations).not.toHaveBeenCalled();
  });

  it('lists your conversations and opens one from the address', async () => {
    await render('c1');
    expect(el.querySelector('.convo-list')?.textContent).toContain('How am I doing?');
    expect(api.conversation).toHaveBeenCalledWith('c1');
    expect(el.querySelector('#chat-title')?.textContent).toContain('How am I doing?');
    expect(el.querySelector('.transcript')?.textContent).toContain('Up 2% this month.');
  });

  it('starts a research-only conversation and streams the answer with its steps', async () => {
    api.send.mockImplementation(() =>
      stream([
        { kind: 'tool_call', data: { id: 't1', name: 'get_portfolio', arguments: {} } },
        { kind: 'tool_result', data: { id: 't1', name: 'get_portfolio', ok: true, result: {} } },
        { kind: 'text', data: { delta: 'You hold' } },
        { kind: 'text', data: { delta: ' cash only.' } },
        { kind: 'done', data: { steps: 2, pending_action_id: null } },
      ]),
    );
    await render();
    const router = TestBed.inject(Router);
    const navigate = vi.spyOn(router, 'navigate');
    const box = el.querySelector<HTMLInputElement>('.research input')!;
    box.checked = true;
    box.dispatchEvent(new Event('change'));
    type('What do I hold?');
    submit();
    await settle(12);

    expect(api.create).toHaveBeenCalledWith(undefined, true);
    expect(api.send).toHaveBeenCalledWith('c2', 'What do I hold?', expect.any(AbortSignal));
    expect(navigate).toHaveBeenCalledWith(['/assistant'], { queryParams: { c: 'c2' } });
    const transcript = el.querySelector('.transcript')!.textContent!;
    expect(transcript).toContain('What do I hold?');
    expect(transcript).toContain('Read your portfolio');
    expect(transcript).toContain('You hold cash only.');
    expect(el.querySelector('[role="status"].visually-hidden')?.textContent).toContain(
      'The answer is complete.',
    );
    expect(el.querySelector<HTMLTextAreaElement>('#assistant-message')!.value).toBe('');
  });

  it('waits for your yes on a write action and follows the rest of the turn', async () => {
    api.send.mockImplementation(() =>
      stream([
        { kind: 'tool_call', data: { id: 't1', name: 'engage_kill_switch', arguments: {} } },
        {
          kind: 'confirm_required',
          data: {
            action_id: 'a1',
            tool: 'engage_kill_switch',
            description: 'Stop new orders',
            arguments: { scope: 'user' },
            preview: { warnings: ['Stops every new order'] },
          },
        },
        { kind: 'done', data: { steps: 1, pending_action_id: 'a1' } },
      ]),
    );
    api.decide.mockImplementation(() =>
      stream([
        { kind: 'tool_result', data: { id: 't1', name: 'engage_kill_switch', ok: true } },
        { kind: 'text', data: { delta: 'Trading is stopped.' } },
        { kind: 'done', data: { steps: 1, pending_action_id: null } },
      ]),
    );
    await render('c1');
    type('Stop trading');
    submit();
    await settle(10);
    expect(el.textContent).toContain('Needs your yes');
    expect(el.querySelector('#assistant-message-hint')?.textContent).toContain('Approve or reject');

    button('Approve and run').click();
    await settle(10);
    expect(api.decide).toHaveBeenCalledWith('c1', 'a1', true, expect.any(AbortSignal));
    expect(el.textContent).toContain('Approved');
    expect(el.textContent).toContain('Trading is stopped.');
    expect(el.querySelector('#assistant-message-hint')?.textContent).toContain('Ctrl+Enter');
  });

  it('shows a refused turn as an error in the chat', async () => {
    api.send.mockImplementation(async function* () {
      await tick();
      throw new Error('the assistant is off');
      yield* [];
    });
    await render('c1');
    type('hello');
    submit();
    await settle(8);
    expect(el.querySelector('.msg.error')?.textContent).toContain('the assistant is off');
  });

  it('shows the trace of a conversation', async () => {
    api.turns.mockResolvedValue([
      {
        id: 't1',
        model: 'llama3.1',
        prompt_version: 'v1',
        status: 'done',
        steps: 1,
        trace: [],
        draft_ids: [],
        started_at: '2026-09-20T10:00:00Z',
        finished_at: null,
      },
    ]);
    await render('c1');
    button('Trace').click();
    await settle();
    expect(api.turns).toHaveBeenCalledWith('c1');
    expect(el.textContent).toContain('Model llama3.1');
  });

  it('shows the freeze and unfreezes after asking', async () => {
    status = { ...ON, frozen_until: '2999-01-01T00:00:00Z', reason: 'too many writes' };
    const success = vi.spyOn(TestBed.inject(ToastService), 'success');
    await render();
    expect(el.querySelector('.banner.warn')?.textContent).toContain('frozen until');
    status = ON;
    button('Unfreeze now').click();
    await settle();
    expect(confirm).toHaveBeenCalled();
    expect(api.clearFreeze).toHaveBeenCalled();
    expect(success).toHaveBeenCalledWith('Unfroze the assistant.');
  });

  it('deletes a conversation after asking', async () => {
    await render('c1');
    el.querySelector<HTMLButtonElement>('[aria-label="Delete How am I doing?"]')!.click();
    await settle();
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ tone: 'danger' }));
    expect(api.delete).toHaveBeenCalledWith('c1');
  });

  it('knows when a freeze is over', () => {
    expect(frozenUntil(null)).toBeNull();
    expect(frozenUntil('2020-01-01T00:00:00Z')).toBeNull();
    expect(frozenUntil('2999-01-01T00:00:00Z')).toBe('2999-01-01T00:00:00Z');
  });
});
