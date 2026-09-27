import { TestBed } from '@angular/core/testing';

import { CopyButton } from './copy-button';

describe('CopyButton', () => {
  function render(writeText: () => Promise<void>) {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const fixture = TestBed.createComponent(CopyButton);
    fixture.componentRef.setInput('text', 'JBSWY3DP');
    fixture.componentRef.setInput('label', 'key');
    fixture.detectChanges();
    return fixture;
  }

  it('copies the text and says so', async () => {
    const writeText = vi.fn(async () => undefined);
    const fixture = render(writeText);
    const el: HTMLElement = fixture.nativeElement;
    el.querySelector('button')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(writeText).toHaveBeenCalledWith('JBSWY3DP');
    expect(el.querySelector('button')!.textContent).toContain('Copied');
    expect(el.querySelector('[role="alert"]')).toBeNull();
  });

  it('says when the clipboard is blocked', async () => {
    const fixture = render(async () => {
      throw new Error('blocked');
    });
    const el: HTMLElement = fixture.nativeElement;
    el.querySelector('button')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Copy is blocked here');
  });
});
