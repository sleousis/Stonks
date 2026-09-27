import { TestBed } from '@angular/core/testing';

import { OneTimeSecret } from './one-time-secret';

describe('OneTimeSecret', () => {
  function render(values: string[]) {
    const fixture = TestBed.createComponent(OneTimeSecret);
    fixture.componentRef.setInput('values', values);
    fixture.componentRef.setInput('label', 'Recovery codes');
    fixture.detectChanges();
    return fixture;
  }

  it('lists several values and copies them one per line', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const fixture = render(['aaaa-1111', 'bbbb-2222']);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('li')).toHaveLength(2);
    el.querySelector('button')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(writeText).toHaveBeenCalledWith('aaaa-1111\nbbbb-2222');
    expect(el.querySelector('button')!.textContent).toContain('Copied');
  });

  it('shows a single value on its own', () => {
    const fixture = render(['stk_abc_123']);
    expect(fixture.nativeElement.querySelector('.single').textContent).toBe('stk_abc_123');
  });
});
