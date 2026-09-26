import { SseParser } from './sse-parser';

describe('SseParser', () => {
  it('parses a complete event', () => {
    const p = new SseParser();
    expect(p.push('event: status\nid: 1\ndata: {"a":1}\n\n')).toEqual([
      { event: 'status', id: '1', data: '{"a":1}' },
    ]);
  });

  it('defaults the event name to "message"', () => {
    expect(new SseParser().push('data: hi\n\n')).toEqual([{ event: 'message', data: 'hi' }]);
  });

  it('joins multi-line data with newlines', () => {
    expect(new SseParser().push('data: a\ndata: b\n\n')[0].data).toBe('a\nb');
  });

  it('handles chunks split anywhere', () => {
    const p = new SseParser();
    const text = 'event: done\ndata: {"status":"succeeded"}\n\n';
    const out = [];
    for (const ch of text) out.push(...p.push(ch));
    expect(out).toEqual([{ event: 'done', data: '{"status":"succeeded"}' }]);
  });

  it('accepts CRLF and CR line endings, including CRLF split across chunks', () => {
    const p = new SseParser();
    expect(p.push('data: one\r')).toEqual([]);
    expect(p.push('\n\r\n')).toEqual([{ event: 'message', data: 'one' }]);
    expect(p.push('data: two\r\r')).toEqual([{ event: 'message', data: 'two' }]);
  });

  it('ignores comments, unknown fields and events without data', () => {
    const p = new SseParser();
    expect(p.push(': keep-alive\n\nfoo: bar\nevent: ping\n\n')).toEqual([]);
    // The event name of a data-less block does not leak into the next event.
    expect(p.push('data: x\n\n')).toEqual([{ event: 'message', data: 'x' }]);
  });

  it('keeps only one leading space of a value and reads retry', () => {
    expect(new SseParser().push('retry: 3000\ndata:  two spaces\n\n')).toEqual([
      { event: 'message', data: ' two spaces', retry: 3000 },
    ]);
  });

  it('returns several events from one chunk', () => {
    const out = new SseParser().push('event: status\ndata: 1\n\nevent: done\ndata: 2\n\n');
    expect(out.map((m) => m.event)).toEqual(['status', 'done']);
  });
});
