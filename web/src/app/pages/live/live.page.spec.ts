import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { StreamStatusView } from '../../api/models';
import { provideApi } from '../../api/provide-api';
import { StreamService } from '../../api/stream.service';
import { nextRequest, tick } from '../../../testing/http';
import { LivePage } from './live.page';
import { engine, status } from './live-test-fixtures';

describe('LivePage', () => {
  let fixture: ComponentFixture<LivePage>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(view: StreamStatusView): Promise<HTMLElement> {
    fixture = TestBed.createComponent(LivePage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/stream/status')).flush(view);
    await tick();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows a running engine with its stream and speed', async () => {
    const el = await render(status());
    const card = el.querySelector('.engine')!;
    expect(card.querySelector('h2')!.textContent).toContain('intraday');
    expect(card.textContent).toContain('Running');
    expect(card.textContent).toContain('Price stream');
    expect(card.textContent).toContain('Connected');
    expect(card.textContent).toContain('3 s ago');
    expect(card.textContent).toContain('175');
    expect(card.textContent).toContain('Median under 50 ms, 95% under 250 ms');
    expect(card.textContent).toContain('Median under 500 ms, 95% under 1 s');
    expect(el.querySelector('.banner')).toBeNull();
  });

  it('flags a silent engine and lists failed steps', async () => {
    const el = await render(
      status({
        engines: [
          engine({ deadman: 'silent', silent_seconds: 420, handler_errors: { decide: 2 } }),
        ],
      }),
    );
    expect(el.querySelector('.banner')!.textContent).toContain('1 engine needs');
    const card = el.querySelector('.engine')!;
    expect(card.getAttribute('data-tone')).toBe('negative');
    expect(card.textContent).toContain('Silent');
    expect(card.textContent).toContain('7 min');
    expect(card.textContent).toContain('Steps that failed');
    expect(card.textContent).toContain('decide');
    expect(card.textContent).toContain('2 times');
  });

  it('shows a stream problem as a note', async () => {
    const base = engine();
    const el = await render(
      status({
        engines: [engine({ stream: { ...base.stream!, last_error: 'connect refused' } })],
      }),
    );
    expect(el.querySelector('.problem[role="note"]')!.textContent).toContain('connect refused');
  });

  it('says when no engine runs and when intraday is off', async () => {
    const el = await render(status({ engines: [], streaming_enabled: false }));
    expect(el.textContent).toContain('No live engine running');
    expect(el.textContent).toContain('Intraday trading is off');
  });

  it('shows the intraday P&L note while live marks are missing', async () => {
    const el = await render(status());
    const pnl = el.querySelector('.pnl')!;
    expect(pnl.querySelector('h2')!.textContent).toContain('Intraday P&L by portfolio');
    expect(pnl.textContent).toContain('21.3.3');
    expect(el.querySelector('.footnote')!.textContent).toContain('5 minutes');
  });

  it('labels each engine section by its heading', async () => {
    const el = await render(status());
    const section = el.querySelector('.engine > section')!;
    const id = section.getAttribute('aria-labelledby')!;
    expect(el.querySelector(`#${id}`)!.textContent).toContain('intraday');
  });

  it('refreshes on demand', async () => {
    const el = await render(status());
    const button = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent!.includes('Refresh'),
    )!;
    button.click();
    fixture.detectChanges();
    (await nextRequest(http, '/api/stream/status')).flush(status({ engines: [] }));
    await tick();
    fixture.detectChanges();
    expect(el.textContent).toContain('No live engine running');
  });

  it('shows an error with a retry', async () => {
    fixture = TestBed.createComponent(LivePage);
    fixture.detectChanges();
    (await nextRequest(http, '/api/stream/status')).flush(
      { title: 'Boom', status: 500, detail: 'down' },
      { status: 500, statusText: 'Server Error' },
    );
    await tick();
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'Could not load the live engine',
    );
  });
});

describe('StreamService', () => {
  it('reads the status route', async () => {
    TestBed.configureTestingModule({ providers: [...provideApi(), provideHttpClientTesting()] });
    const http = TestBed.inject(HttpTestingController);
    const pending = TestBed.inject(StreamService).status();
    (await nextRequest(http, '/api/stream/status')).flush(status());
    expect((await pending).deadman_minutes).toBe(5);
    http.verify();
  });
});
