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

  it('clipboard rejects: message visible (UX-50)', async () => {
    const writeText = vi.fn(async () => {
      throw new Error('blocked');
    });
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const fixture = render(['aaaa-1111']);
    const el: HTMLElement = fixture.nativeElement;
    el.querySelector('button')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Copy is blocked here');
  });

  it('offers Download .txt when given a file name (UX-50)', () => {
    const fixture = TestBed.createComponent(OneTimeSecret);
    fixture.componentRef.setInput('values', ['a', 'b']);
    fixture.componentRef.setInput('filename', 'stonks-recovery-codes.txt');
    fixture.detectChanges();
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:x');
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const buttons = [...fixture.nativeElement.querySelectorAll('button')] as HTMLButtonElement[];
    buttons.find((b) => b.textContent?.includes('Download .txt'))!.click();
    expect(create).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    create.mockRestore();
    revoke.mockRestore();
    click.mockRestore();
  });

  it('shows a single value on its own', () => {
    const fixture = render(['stk_abc_123']);
    expect(fixture.nativeElement.querySelector('.single').textContent).toBe('stk_abc_123');
  });
});
