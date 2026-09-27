import { TestBed } from '@angular/core/testing';

import type { ConfirmItem } from './chat-model';
import { ConfirmStep, previewLines } from './confirm-step';

const CONFIRM: ConfirmItem = {
  type: 'confirm',
  key: 'k1',
  actionId: 'a1',
  tool: 'engage_kill_switch',
  description: 'Stop new orders at once.',
  args: { scope: 'user', reason: 'news' },
  preview: { preview: true, warnings: ['This stops every new order in all of your portfolios.'] },
  state: 'pending',
};

describe('ConfirmStep', () => {
  function render(item: ConfirmItem, busy = false) {
    const fixture = TestBed.createComponent(ConfirmStep);
    fixture.componentRef.setInput('item', item);
    fixture.componentRef.setInput('busy', busy);
    fixture.detectChanges();
    return fixture;
  }

  it('shows the action as a ticket with its inputs and preview', () => {
    const el = render(CONFIRM).nativeElement as HTMLElement;
    expect(el.querySelector('h3')?.textContent).toContain('Stop trading');
    expect(el.textContent).toContain('Stop new orders at once.');
    expect(el.textContent).toContain('Scope');
    expect(el.textContent).toContain('This stops every new order');
    expect(el.textContent).toContain('Nothing runs until you approve it.');
    expect(el.querySelector('.btn-danger')?.textContent).toContain('Approve and run');
  });

  it('emits the answer and hides the buttons once decided', () => {
    const fixture = render(CONFIRM);
    const answers: boolean[] = [];
    fixture.componentInstance.decide.subscribe((v) => answers.push(v));
    const buttons = [...(fixture.nativeElement as HTMLElement).querySelectorAll('button')];
    buttons.find((b) => b.textContent?.includes('Reject'))!.click();
    buttons.find((b) => b.textContent?.includes('Approve'))!.click();
    expect(answers).toEqual([false, true]);

    fixture.componentRef.setInput('item', { ...CONFIRM, state: 'rejected' });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('button')).toHaveLength(0);
    expect(el.textContent).toContain('Rejected');
  });

  it('disables the buttons while busy', () => {
    const el = render(CONFIRM, true).nativeElement as HTMLElement;
    expect([...el.querySelectorAll('button')].every((b) => b.disabled)).toBe(true);
  });

  it('reads warnings and summaries out of a preview', () => {
    expect(previewLines({ summary: 'A', warnings: ['B', 3] })).toEqual(['A', 'B']);
    expect(previewLines('plain')).toEqual(['plain']);
    expect(previewLines(null)).toEqual([]);
  });
});
