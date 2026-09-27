import type { AssistantEvent } from '../../api/assistant.service';
import type { ConversationDetailView, MessageView, PendingActionView } from '../../api/models';

type Args = Record<string, unknown>;

/** A tool the assistant used, shown as one step of the answer. */
export interface StepItem {
  type: 'step';
  key: string;
  callId: string;
  tool: string;
  args: Args;
  /**
   * running: called, no answer yet. waiting: needs your yes or no.
   * done / failed: the tool answered. declined: you said no, nothing ran.
   */
  state: 'running' | 'waiting' | 'done' | 'failed' | 'declined';
  result?: unknown;
  error?: string;
}

/** A write action waiting for your yes or no. */
export interface ConfirmItem {
  type: 'confirm';
  key: string;
  actionId: string;
  tool: string;
  description: string;
  args: Args;
  preview: unknown;
  state: 'pending' | 'approved' | 'rejected';
}

export interface UserItem {
  type: 'user';
  key: string;
  text: string;
}

export interface AnswerItem {
  type: 'answer';
  key: string;
  text: string;
  streaming: boolean;
}

export interface ErrorItem {
  type: 'error';
  key: string;
  code: string;
  message: string;
}

export type ChatItem = UserItem | AnswerItem | StepItem | ConfirmItem | ErrorItem;

/** The loop's answer when you say no (`stonks.assistant.loop.DECLINED`, `MOVED_ON`). */
const NOT_RUN = /^The person (declined|sent a new message)/;

let seq = 0;
function key(prefix: string): string {
  seq += 1;
  return `${prefix}-${seq}`;
}

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function args(value: unknown): Args {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Args)
    : {};
}

function endStreaming(items: ChatItem[]): ChatItem[] {
  return items.map((i) => (i.type === 'answer' && i.streaming ? { ...i, streaming: false } : i));
}

/** The last step for this tool call, searched from the end. */
function stepIndex(items: readonly ChatItem[], callId: string, tool?: string): number {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type !== 'step') continue;
    if (item.callId === callId && (tool === undefined || item.tool === tool)) return i;
  }
  return -1;
}

/** Your own message, shown at once while the answer streams. */
export function userItem(text: string): UserItem {
  return { type: 'user', key: key('user'), text };
}

/** Fold one server-sent event into the transcript (a new array, never in place). */
export function applyEvent(items: readonly ChatItem[], event: AssistantEvent): ChatItem[] {
  const data = event.data ?? {};
  switch (event.kind) {
    case 'text': {
      const delta = str(data['delta']);
      if (!delta) return [...items];
      const last = items[items.length - 1];
      if (last?.type === 'answer' && last.streaming) {
        return [...items.slice(0, -1), { ...last, text: last.text + delta }];
      }
      return [...items, { type: 'answer', key: key('answer'), text: delta, streaming: true }];
    }
    case 'tool_call': {
      const next = endStreaming([...items]);
      next.push({
        type: 'step',
        key: key('step'),
        callId: str(data['id']),
        tool: str(data['name'], 'tool'),
        args: args(data['arguments']),
        state: 'running',
      });
      return next;
    }
    case 'tool_result': {
      const next = [...items];
      const at = stepIndex(next, str(data['id']), str(data['name']) || undefined);
      const ok = data['ok'] === true;
      const error = ok ? undefined : str(data['error'], 'The tool failed.');
      const patch: Partial<StepItem> = ok
        ? { state: 'done', result: data['result'], error: undefined }
        : { state: error && NOT_RUN.test(error) ? 'declined' : 'failed', error };
      if (at >= 0) {
        next[at] = { ...(next[at] as StepItem), ...patch };
      } else {
        next.push({
          type: 'step',
          key: key('step'),
          callId: str(data['id']),
          tool: str(data['name'], 'tool'),
          args: {},
          state: 'running',
          ...patch,
        });
      }
      return next;
    }
    case 'confirm_required': {
      const next = endStreaming([...items]);
      const tool = str(data['tool'], 'tool');
      for (let i = next.length - 1; i >= 0; i--) {
        const item = next[i];
        if (item.type === 'step' && item.tool === tool && item.state === 'running') {
          next[i] = { ...item, state: 'waiting' };
          break;
        }
      }
      next.push({
        type: 'confirm',
        key: key('confirm'),
        actionId: str(data['action_id']),
        tool,
        description: str(data['description']),
        args: args(data['arguments']),
        preview: data['preview'] ?? null,
        state: 'pending',
      });
      return next;
    }
    case 'error':
      return [
        ...endStreaming([...items]),
        {
          type: 'error',
          key: key('error'),
          code: str(data['code'], 'error'),
          message: str(data['message'], 'The assistant stopped.'),
        },
      ];
    case 'done':
      return endStreaming([...items]);
    default:
      return [...items];
  }
}

/** Mark a confirm step as decided (before the rest of the turn streams in). */
export function decideItem(
  items: readonly ChatItem[],
  actionId: string,
  approve: boolean,
): ChatItem[] {
  return items.map((i) => {
    if (i.type === 'confirm' && i.actionId === actionId) {
      return { ...i, state: approve ? 'approved' : 'rejected' };
    }
    return i;
  });
}

/** The action still waiting for your answer, if any. */
export function pendingConfirm(items: readonly ChatItem[]): ConfirmItem | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.type === 'confirm' && item.state === 'pending') return item;
  }
  return null;
}

function toolPayload(message: MessageView): { ok: boolean; result?: unknown; error?: string } {
  try {
    const parsed = JSON.parse(message.content) as unknown;
    if (typeof parsed === 'object' && parsed !== null && 'ok' in parsed) {
      const p = parsed as { ok: unknown; result?: unknown; error?: unknown };
      return p.ok === true
        ? { ok: true, result: p.result }
        : { ok: false, error: str(p.error, 'The tool failed.') };
    }
    return { ok: true, result: parsed };
  } catch {
    return { ok: true, result: message.content };
  }
}

/** Rebuild the transcript of a stored conversation, pending actions included. */
export function fromHistory(detail: ConversationDetailView): ChatItem[] {
  let items: ChatItem[] = [];
  for (const m of detail.messages) {
    if (m.role === 'user') {
      items.push({ type: 'user', key: `m${m.id}`, text: m.content });
    } else if (m.role === 'assistant') {
      if (m.content.trim()) {
        items.push({ type: 'answer', key: `m${m.id}`, text: m.content, streaming: false });
      }
      for (const call of m.tool_calls ?? []) {
        items.push({
          type: 'step',
          key: `m${m.id}-${call.id}`,
          callId: call.id,
          tool: call.name,
          args: args(call.arguments),
          state: 'running',
        });
      }
    } else {
      const payload = toolPayload(m);
      items = applyEvent(items, {
        kind: 'tool_result',
        data: { id: m.tool_call_id ?? '', name: m.tool_name ?? '', ...payload },
      });
    }
  }
  for (const action of detail.pending_actions.filter((a) => a.status === 'pending')) {
    items = addPending(items, action);
  }
  // A call with no answer and no pending action was cut off (a stopped turn).
  return items.map((i) =>
    i.type === 'step' && i.state === 'running'
      ? { ...i, state: 'failed', error: 'No answer: the turn stopped here.' }
      : i,
  );
}

function addPending(items: ChatItem[], action: PendingActionView): ChatItem[] {
  const next = [...items];
  const at = stepIndex(next, action.tool_call_id);
  if (at >= 0) next[at] = { ...(next[at] as StepItem), state: 'waiting' };
  next.push({
    type: 'confirm',
    key: `action-${action.id}`,
    actionId: action.id,
    tool: action.tool_name,
    description: '',
    args: args(action.arguments),
    preview: null,
    state: 'pending',
  });
  return next;
}
