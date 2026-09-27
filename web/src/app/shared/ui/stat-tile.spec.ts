import { TestBed } from '@angular/core/testing';

import { StatTile } from './stat-tile';

describe('StatTile', () => {
  function render(value: string) {
    const fixture = TestBed.createComponent(StatTile);
    fixture.componentRef.setInput('label', 'Leading');
    fixture.componentRef.setInput('value', value);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('lets a 34-character name wrap instead of scrolling sideways (UX-27)', () => {
    const el = render('stocks_on_the_move_long_3fa9c21b_x');
    expect(el.classList).toContain('text');
    expect(el.querySelector('.value')!.textContent).toContain('stocks_on_the_move');
  });

  it('keeps a figure on one line', () => {
    expect(render('+5.24%').classList).not.toContain('text');
    expect(render('$12,400').classList).not.toContain('text');
  });
});
