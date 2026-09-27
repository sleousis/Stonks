import { Injectable, InjectionToken, inject } from '@angular/core';

import { AuthTokenService } from '../core/auth/auth-token.service';
import { CSRF_HEADER } from '../core/auth/session.interceptor';
import { SessionService } from '../core/auth/session.service';
import { ApiError, toApiError } from '../core/http/api-error';
import { allItems, unwrap } from './api-call';
import {
  clearAssistantFreeze,
  createAssistantConversation,
  decideAssistantAction,
  deleteAssistantConversation,
  getAssistantConversation,
  getAssistantStatus,
  listAssistantConversations,
  listAssistantTurns,
  sendAssistantMessage,
} from './generated/sdk.gen';
import type { AssistantEventView } from './generated/types.gen';

/** The fetch the event streams use (tests swap it for canned streams). */
export const ASSISTANT_FETCH = new InjectionToken<typeof fetch>('ASSISTANT_FETCH', {
  providedIn: 'root',
  factory: () => (input, init) => globalThis.fetch(input, init),
});

/** One event of a turn, as the server sends it. */
export type AssistantEvent = AssistantEventView;

interface StreamResult {
  stream: AsyncGenerator<unknown>;
}

/**
 * The in-app AI assistant: its status (off until a model endpoint is set),
 * your conversations, and the two streamed calls. `send` and `decide`
 * answer with server-sent events (`text`, `tool_call`, `tool_result`,
 * `confirm_required`, `error`, then `done`). A POST must never be sent
 * twice, so the stream is not retried.
 *
 * The streams go through `fetch`, not HttpClient, so the interceptors do
 * not see them: this service adds the credential (the tab's API token, else
 * the CSRF header for the session cookie) and turns a refused request into
 * an ApiError with the API's message.
 */
@Injectable({ providedIn: 'root' })
export class AssistantService {
  private readonly tokens = inject(AuthTokenService);
  private readonly session = inject(SessionService);
  private readonly fetchImpl = inject(ASSISTANT_FETCH);

  status() {
    return unwrap(getAssistantStatus());
  }

  conversations() {
    return allItems((query) => unwrap(listAssistantConversations({ query })));
  }

  conversation(id: string) {
    return unwrap(getAssistantConversation({ path: { conversation_id: id } }));
  }

  create(title?: string, researchOnly = false) {
    return unwrap(createAssistantConversation({ body: { title, research_only: researchOnly } }));
  }

  /** The recorded turns of a conversation: model, prompt, tool calls, drafts. */
  turns(id: string) {
    return allItems((query) =>
      unwrap(listAssistantTurns({ path: { conversation_id: id }, query })),
    );
  }

  /** Unfreeze after a burst of writes (asks for a fresh second factor). */
  clearFreeze() {
    return unwrap(clearAssistantFreeze());
  }

  delete(id: string) {
    return unwrap(deleteAssistantConversation({ path: { conversation_id: id } }));
  }

  /** Send a message and follow the events of this turn. */
  send(id: string, content: string, signal?: AbortSignal): AsyncGenerator<AssistantEvent> {
    const body = { content };
    return this.follow(body, (extra) =>
      sendAssistantMessage({ path: { conversation_id: id }, body, signal, ...extra }),
    );
  }

  /** Approve or reject a pending write action and follow the rest of the turn. */
  decide(
    id: string,
    actionId: string,
    approve: boolean,
    signal?: AbortSignal,
  ): AsyncGenerator<AssistantEvent> {
    const body = { approve };
    return this.follow(body, (extra) =>
      decideAssistantAction({
        path: { conversation_id: id, action_id: actionId },
        body,
        signal,
        ...extra,
      }),
    );
  }

  private headers(): Record<string, string> {
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    };
    const token = this.tokens.token();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    } else {
      const csrf = this.session.csrfToken();
      if (csrf) headers[CSRF_HEADER] = csrf;
    }
    return headers;
  }

  private async *follow(
    body: object,
    open: (extra: {
      baseUrl: string;
      onRequest: (url: string, init: RequestInit) => Promise<Request>;
      credentials: RequestCredentials;
      fetch: typeof fetch;
      sseMaxRetryAttempts: number;
      onSseError: (error: unknown) => void;
    }) => Promise<StreamResult>,
  ): AsyncGenerator<AssistantEvent> {
    let refused: ApiError | null = null;
    let failed: unknown = null;
    const fetchImpl = this.fetchImpl;
    const guarded: typeof fetch = async (input, init) => {
      const response = await fetchImpl(input, init);
      if (!response.ok) refused = await problemOf(response);
      return response;
    };
    const headers = this.headers();
    const result = await open({
      // The Angular client leaves the headers and the body as Angular objects,
      // which fetch cannot read: set both here.
      onRequest: async (url, init) =>
        new Request(url, { ...init, headers, body: JSON.stringify(body) }),
      // fetch needs an absolute URL outside a page (tests); same origin in the app.
      baseUrl: globalThis.location?.origin ?? '',
      credentials: 'same-origin',
      fetch: guarded,
      sseMaxRetryAttempts: 1,
      onSseError: (error) => (failed = error),
    });
    let sawDone = false;
    for await (const item of result.stream) {
      const event = asEvent(item);
      if (!event) continue;
      if (event.kind === 'done') sawDone = true;
      yield event;
    }
    if (refused) throw refused;
    if (!sawDone && failed !== null && !isAbort(failed)) {
      throw new ApiError(
        0,
        'Network error',
        'The answer stopped before it finished. Check your connection, then reload the conversation.',
      );
    }
  }
}

function asEvent(item: unknown): AssistantEvent | null {
  if (typeof item !== 'object' || item === null) return null;
  const { kind, data } = item as { kind?: unknown; data?: unknown };
  if (typeof kind !== 'string') return null;
  return {
    kind: kind as AssistantEvent['kind'],
    data: typeof data === 'object' && data !== null ? (data as AssistantEvent['data']) : {},
  };
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

async function problemOf(response: Response): Promise<ApiError> {
  let text = '';
  try {
    text = await response.clone().text();
  } catch {
    // No body to read: the status says enough.
  }
  let body: unknown = text;
  try {
    body = JSON.parse(text);
  } catch {
    // Not JSON: keep the text.
  }
  return toApiError(body, { status: response.status });
}
