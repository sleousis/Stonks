import { TestBed } from '@angular/core/testing';

import type { TurnView } from '../../api/models';
import { TraceSheet, traceRow, turnMeta, turnStatusText } from './trace-sheet';

describe('TraceSheet', () => {
  const TURN: TurnView = {
    id: 't1',
    model: 'llama3.1',
    prompt_version: 'v3',
    status: 'paused',
    steps: 2,
    started_at: '2026-09-20T10:00:00Z',
    finished_at: '2026-09-20T10:00:05Z',
    draft_ids: ['d1'],
    trace: [
      { tool: 'get_portfolio', arguments: {}, ok: true, result: '{"cash": 1}' },
      { tool: 'engage_kill_switch', arguments: { scope: 'user' }, pending_action: 'a1' },
      { tool: 'get_bars', arguments: { ticker: 'X' }, ok: false, error: 'no data' },
    ],
  };

  it('says how each turn ended and what each tool call did', () => {
    expect(turnStatusText('paused')).toBe('Waited for your yes');
    expect(turnStatusText('something_new')).toBe('Stopped');
    expect(turnMeta(TURN)).toBe('Model llama3.1, prompt v3, 2 steps, 1 order draft');
    expect(traceRow(TURN.trace[1]).outcome).toBe('Asked for your yes');
    expect(traceRow({ ...TURN.trace[0], confirmed: true }).outcome).toBe('Ran after your yes');
    expect(traceRow(TURN.trace[2])).toMatchObject({ outcome: 'Failed', detail: 'no data' });
  });

  it('lists the turns when open', () => {
    const fixture = TestBed.createComponent(TraceSheet);
    fixture.componentRef.setInput('open', true);
    fixture.componentRef.setInput('turns', [TURN]);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('How the assistant got there');
    expect(el.textContent).toContain('Model llama3.1');
    expect(el.textContent).toContain('Read your portfolio');
    expect(el.textContent).toContain('Asked for your yes');
  });

  it('shows loading, then an empty state', () => {
    const fixture = TestBed.createComponent(TraceSheet);
    fixture.componentRef.setInput('open', true);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Loading the trace');
    fixture.componentRef.setInput('turns', []);
    fixture.detectChanges();
    expect(el.textContent).toContain('No turns yet');
  });
});
