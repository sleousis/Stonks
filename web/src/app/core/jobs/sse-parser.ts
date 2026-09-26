/** One dispatched server-sent event. */
export interface SseMessage {
  event: string;
  data: string;
  id?: string;
  retry?: number;
}

/**
 * Incremental parser for the `text/event-stream` format (WHATWG HTML spec,
 * "Parsing an event stream"). Feed it decoded text chunks as they arrive; a
 * chunk may end anywhere, even mid-line or between `\r` and `\n`.
 */
export class SseParser {
  private buffer = '';
  private data: string[] = [];
  private event = '';
  private id: string | undefined;
  private retry: number | undefined;
  private pendingCR = false;

  push(chunk: string): SseMessage[] {
    const out: SseMessage[] = [];
    let text = chunk;
    // A `\r\n` split across chunks: the `\n` belongs to the line already ended.
    if (this.pendingCR && text.startsWith('\n')) text = text.slice(1);
    this.pendingCR = false;
    this.buffer += text;

    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const ch = this.buffer[i];
      if (ch !== '\n' && ch !== '\r') continue;
      const line = this.buffer.slice(start, i);
      if (ch === '\r') {
        if (i + 1 < this.buffer.length) {
          if (this.buffer[i + 1] === '\n') i++;
        } else {
          this.pendingCR = true;
        }
      }
      start = i + 1;
      const msg = this.line(line);
      if (msg) out.push(msg);
    }
    this.buffer = this.buffer.slice(start);
    return out;
  }

  private line(line: string): SseMessage | null {
    if (line === '') return this.dispatch();
    if (line.startsWith(':')) return null; // comment / keep-alive
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'event':
        this.event = value;
        break;
      case 'data':
        this.data.push(value);
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
    }
    return null;
  }

  private dispatch(): SseMessage | null {
    if (this.data.length === 0) {
      this.event = '';
      return null;
    }
    const msg: SseMessage = { event: this.event || 'message', data: this.data.join('\n') };
    if (this.id !== undefined) msg.id = this.id;
    if (this.retry !== undefined) msg.retry = this.retry;
    this.data = [];
    this.event = '';
    return msg;
  }
}
