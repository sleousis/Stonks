import { TestBed } from '@angular/core/testing';

import { ModeStamp } from './mode-stamp';
import { SideTag } from './side-tag';

describe('SideTag', () => {
  function render(side: string | null) {
    const fixture = TestBed.createComponent(SideTag);
    fixture.componentRef.setInput('side', side);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows a solid B for a buy and says "Buy"', () => {
    const el = render('buy');
    const tag = el.querySelector('.tag')!;
    expect(tag.getAttribute('data-side')).toBe('buy');
    expect(tag.querySelector('[aria-hidden]')!.textContent).toBe('B');
    expect(tag.querySelector('.visually-hidden')!.textContent).toBe('Buy');
  });

  it('shows an outlined S for a sell, whatever the case', () => {
    const tag = render('SELL').querySelector('.tag')!;
    expect(tag.getAttribute('data-side')).toBe('sell');
    expect(tag.textContent).toContain('S');
    expect(tag.textContent).toContain('Sell');
  });

  it('copes with an unknown side', () => {
    const tag = render('short').querySelector('.tag')!;
    expect(tag.getAttribute('data-side')).toBe('other');
    expect(tag.querySelector('[aria-hidden]')!.textContent).toBe('S');
  });
});

describe('ModeStamp', () => {
  function render(live: boolean) {
    const fixture = TestBed.createComponent(ModeStamp);
    fixture.componentRef.setInput('live', live);
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement).querySelector('.stamp')!;
  }

  it('stamps PAPER for simulated money', () => {
    const stamp = render(false);
    expect(stamp.getAttribute('data-mode')).toBe('paper');
    expect(stamp.textContent).toContain('PAPER');
    expect(stamp.textContent).toContain('simulated money');
  });

  it('stamps LIVE for real money', () => {
    const stamp = render(true);
    expect(stamp.getAttribute('data-mode')).toBe('live');
    expect(stamp.textContent).toContain('LIVE');
    expect(stamp.textContent).toContain('real money');
  });
});
