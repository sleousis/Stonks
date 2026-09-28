import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { AuthTokenService } from '../core/auth/auth-token.service';
import { SessionService } from '../core/auth/session.service';
import { ApiError } from '../core/http/api-error';
import { ASSISTANT_FETCH, type AssistantEvent, AssistantService } from './assistant.service';
import { provideApi } from './provide-api';

function sse(events: { kind: string; data: Record<string, unknown> }[]): string {
  return events
    .map((e, i) => `event: ${e.kind}\nid: ${i + 1}\ndata: ${JSON.stringify(e)}\n\n`)
    .join('');
}

function streamResponse(body: string, status = 200): Response {
  const bytes = new TextEncoder().encode(body);
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      // Two chunks, split mid-event, as a network would.
      const half = Math.floor(bytes.length / 2);
      controller.enqueue(bytes.slice(0, half));
      controller.enqueue(bytes.slice(half));
      controller.close();
    },
  });
  return new Response(stream, {
    status,
    headers: { 'Content-Type': status < 300 ? 'text/event-stream' : 'application/json' },
  });
}

async function collect(gen: AsyncGenerator<AssistantEvent>): Promise<AssistantEvent[]> {
  const out: AssistantEvent[] = [];
  for await (const e of gen) out.push(e);
  return out;
}

describe('AssistantService streams', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let token: string | null;
  let csrf: string | null;

  beforeEach(() => {
    token = null;
    csrf = 'csrf-123';
    fetchMock = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        ...provideApi(),
        provideHttpClientTesting(),
        { provide: ASSISTANT_FETCH, useValue: fetchMock },
        { provide: AuthTokenService, useValue: { token: () => token } },
        { provide: SessionService, useValue: { csrfToken: () => csrf } },
      ],
    });
  });

  it('sends the message with the CSRF header and yields each event', async () => {
    fetchMock.mockResolvedValue(
      streamResponse(
        sse([
          { kind: 'text', data: { delta: 'Hello' } },
          { kind: 'text', data: { delta: ' there' } },
          { kind: 'done', data: { conversation_id: 'c1', steps: 1, pending_action_id: null } },
        ]),
      ),
    );
    const api = TestBed.inject(AssistantService);
    const events = await collect(api.send('c1', 'hi'));

    expect(events.map((e) => e.kind)).toEqual(['text', 'text', 'done']);
    expect(events[1].data['delta']).toBe(' there');
    const request = fetchMock.mock.calls[0][0] as Request;
    expect(request.method).toBe('POST');
    expect(new URL(request.url, 'http://x').pathname).toBe(
      '/api/assistant/conversations/c1/messages',
    );
    expect(request.headers.get('X-CSRF-Token')).toBe('csrf-123');
    expect(request.headers.get('Authorization')).toBeNull();
    expect(await request.clone().json()).toEqual({ content: 'hi' });
  });

  it('uses the tab API token instead of the CSRF header', async () => {
    token = 'tok';
    fetchMock.mockResolvedValue(streamResponse(sse([{ kind: 'done', data: {} }])));
    const api = TestBed.inject(AssistantService);
    await collect(api.decide('c1', 'a1', true));
    const request = fetchMock.mock.calls[0][0] as Request;
    expect(new URL(request.url, 'http://x').pathname).toBe(
      '/api/assistant/conversations/c1/actions/a1',
    );
    expect(request.headers.get('Authorization')).toBe('Bearer tok');
    expect(request.headers.get('X-CSRF-Token')).toBeNull();
    expect(await request.clone().json()).toEqual({ approve: true });
  });

  it('turns a refused request into an ApiError with the API message, sent once', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          type: 'about:blank',
          title: 'Conflict',
          status: 409,
          detail: 'the assistant is off',
        }),
        { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
      ),
    );
    const api = TestBed.inject(AssistantService);
    const failure = await collect(api.send('c1', 'hi')).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(409);
    expect((failure as ApiError).message).toContain('the assistant is off');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('says so when the stream breaks before the turn ends', async () => {
    fetchMock.mockRejectedValue(new TypeError('network down'));
    const api = TestBed.inject(AssistantService);
    const failure = await collect(api.send('c1', 'hi')).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toContain('stopped before it finished');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
