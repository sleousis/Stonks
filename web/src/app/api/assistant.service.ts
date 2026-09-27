import { Injectable } from '@angular/core';

import { allItems, unwrap } from './api-call';
import {
  createAssistantConversation,
  decideAssistantAction,
  deleteAssistantConversation,
  getAssistantConversation,
  getAssistantStatus,
  listAssistantConversations,
  sendAssistantMessage,
} from './generated/sdk.gen';

/**
 * The in-app AI assistant: its status (off until a model endpoint is set),
 * your conversations, and the two streamed calls. `send` and `decide`
 * answer with server-sent events (`text`, `tool_call`, `tool_result`,
 * `confirm_required`, `error`, then `done`). A POST must never be sent
 * twice, so the stream is not retried.
 */
@Injectable({ providedIn: 'root' })
export class AssistantService {
  status() {
    return unwrap(getAssistantStatus());
  }

  conversations() {
    return allItems((query) => unwrap(listAssistantConversations({ query })));
  }

  conversation(id: string) {
    return unwrap(getAssistantConversation({ path: { conversation_id: id } }));
  }

  create(title?: string) {
    return unwrap(createAssistantConversation({ body: { title } }));
  }

  delete(id: string) {
    return unwrap(deleteAssistantConversation({ path: { conversation_id: id } }));
  }

  /** Send a message; iterate `.stream` for the events of this turn. */
  send(id: string, content: string, signal?: AbortSignal) {
    return sendAssistantMessage({
      path: { conversation_id: id },
      body: { content },
      signal,
      sseMaxRetryAttempts: 1,
    });
  }

  /** Approve or reject a pending write action; the turn goes on in `.stream`. */
  decide(id: string, actionId: string, approve: boolean, signal?: AbortSignal) {
    return decideAssistantAction({
      path: { conversation_id: id, action_id: actionId },
      body: { approve },
      signal,
      sseMaxRetryAttempts: 1,
    });
  }
}
