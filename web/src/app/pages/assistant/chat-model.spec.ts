import type { AssistantEvent } from '../../api/assistant.service';
import type { ConversationDetailView } from '../../api/models';
import {
  type ChatItem,
  type ConfirmItem,
  type StepItem,
  applyEvent,
  decideItem,
  fromHistory,
  pendingConfirm,
  userItem,
} from './chat-model';

function run(events: AssistantEvent[], start: ChatItem[] = []): ChatItem[] {
  return events.reduce(applyEvent, start);
}

const ev = (kind: AssistantEvent['kind'], data: Record<string, unknown> = {}): AssistantEvent => ({
  kind,
  data,
});

describe('chat model', () => {
  it('joins streamed text into one answer and ends it on done', () => {
    const items = run([
      ev('text', { delta: 'Your' }),
      ev('text', { delta: ' book is up' }),
      ev('done', { steps: 1 }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ type: 'answer', text: 'Your book is up', streaming: false });
  });

  it('shows each tool call as a step and settles it with its result', () => {
    const items = run([
      ev('text', { delta: 'Checking.' }),
      ev('tool_call', { id: 'c1', name: 'get_portfolio', arguments: {} }),
      ev('tool_result', { id: 'c1', name: 'get_portfolio', ok: true, result: { cash: 5 } }),
      ev('tool_call', { id: 'c2', name: 'get_bars', arguments: { ticker: 'AAA.US' } }),
      ev('tool_result', { id: 'c2', name: 'get_bars', ok: false, error: 'no data' }),
      ev('text', { delta: 'Done.' }),
    ]);
    expect(items.map((i) => i.type)).toEqual(['answer', 'step', 'step', 'answer']);
    expect(items[0]).toMatchObject({ streaming: false });
    expect(items[1]).toMatchObject({ state: 'done', result: { cash: 5 } });
    expect(items[2]).toMatchObject({
      state: 'failed',
      error: 'no data',
      args: { ticker: 'AAA.US' },
    });
    expect(items[3]).toMatchObject({ text: 'Done.', streaming: true });
  });

  it('turns a write into a waiting step and a confirm item', () => {
    const items = run([
      ev('tool_call', { id: 'c1', name: 'engage_kill_switch', needs_confirmation: true }),
      ev('confirm_required', {
        action_id: 'a1',
        tool: 'engage_kill_switch',
        description: 'Stop new orders',
        arguments: { scope: 'user' },
        preview: { warnings: ['Stops every new order'] },
      }),
      ev('done', { pending_action_id: 'a1' }),
    ]);
    expect((items[0] as StepItem).state).toBe('waiting');
    const confirm = items[1] as ConfirmItem;
    expect(confirm).toMatchObject({ type: 'confirm', actionId: 'a1', state: 'pending' });
    expect(pendingConfirm(items)?.actionId).toBe('a1');

    const decided = decideItem(items, 'a1', false);
    expect(pendingConfirm(decided)).toBeNull();
    const after = run(
      [
        ev('tool_result', {
          id: 'c1',
          name: 'engage_kill_switch',
          ok: false,
          error: 'The person declined this action. It was not run.',
        }),
      ],
      decided,
    );
    expect((after[0] as StepItem).state).toBe('declined');
    expect((after[1] as ConfirmItem).state).toBe('rejected');
  });

  it('adds errors as their own item', () => {
    const items = run([
      ev('error', { code: 'timeout', message: 'The assistant ran out of time.' }),
    ]);
    expect(items[0]).toMatchObject({ type: 'error', code: 'timeout' });
  });

  it('never changes the list it was given', () => {
    const start = [userItem('hi')];
    const copy = JSON.stringify(start);
    run([ev('text', { delta: 'x' }), ev('done')], start);
    expect(JSON.stringify(start)).toBe(copy);
  });

  it('rebuilds a stored conversation with its pending action', () => {
    const detail: ConversationDetailView = {
      id: 'c',
      title: 't',
      created_at: '2026-09-01T00:00:00Z',
      updated_at: '2026-09-01T00:00:00Z',
      messages: [
        { id: 1, role: 'user', content: 'How am I doing?', created_at: '' },
        {
          id: 2,
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 't1', name: 'get_portfolio', arguments: {} }],
          created_at: '',
        },
        {
          id: 3,
          role: 'tool',
          content: JSON.stringify({ ok: true, result: { cash: 1 } }),
          tool_call_id: 't1',
          tool_name: 'get_portfolio',
          created_at: '',
        },
        { id: 4, role: 'assistant', content: 'Fine.', created_at: '' },
        { id: 5, role: 'user', content: 'Stop trading', created_at: '' },
        {
          id: 6,
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 't2', name: 'engage_kill_switch', arguments: { scope: 'user' } }],
          created_at: '',
        },
      ],
      pending_actions: [
        {
          id: 'a9',
          tool_call_id: 't2',
          tool_name: 'engage_kill_switch',
          arguments: { scope: 'user' },
          status: 'pending',
          created_at: '',
        },
      ],
    };
    const items = fromHistory(detail);
    expect(items.map((i) => i.type)).toEqual(['user', 'step', 'answer', 'user', 'step', 'confirm']);
    expect((items[1] as StepItem).state).toBe('done');
    expect((items[4] as StepItem).state).toBe('waiting');
    expect(pendingConfirm(items)?.actionId).toBe('a9');
  });

  it('marks a stored call with no answer as cut off', () => {
    const items = fromHistory({
      id: 'c',
      title: '',
      created_at: '',
      updated_at: '',
      messages: [
        {
          id: 1,
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 't1', name: 'get_pnl', arguments: {} }],
          created_at: '',
        },
      ],
      pending_actions: [],
    });
    expect(items[0]).toMatchObject({ type: 'step', state: 'failed' });
  });
});
