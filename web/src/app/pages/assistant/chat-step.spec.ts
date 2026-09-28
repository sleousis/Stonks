import { TestBed } from '@angular/core/testing';

import type { StepItem } from './chat-model';
import { ChatStep } from './chat-step';

const STEP: StepItem = {
  type: 'step',
  key: 's1',
  callId: 'c1',
  tool: 'get_portfolio',
  args: { portfolio_id: 'pf_1' },
  state: 'done',
  result: { cash: 1234 },
};

describe('ChatStep', () => {
  it('shows what the tool did, its state as a word, and a fold with what it saw', () => {
    const fixture = TestBed.createComponent(ChatStep);
    fixture.componentRef.setInput('step', STEP);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Read your portfolio');
    expect(el.textContent).toContain('Done');
    expect(el.querySelector('summary')?.textContent).toContain('Details');
    expect(el.textContent).toContain('Portfolio id');
    expect(el.querySelector('pre')?.textContent).toContain('1234');
  });

  it('shows the error of a failed step', () => {
    const fixture = TestBed.createComponent(ChatStep);
    fixture.componentRef.setInput('step', {
      ...STEP,
      state: 'failed',
      result: undefined,
      error: 'no data',
    });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Failed');
    expect(el.textContent).toContain('no data');
    expect(el.querySelector('pre')).toBeNull();
  });
});
