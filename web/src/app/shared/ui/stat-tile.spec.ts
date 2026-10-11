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

  it('drops its box when bare, for a divided strip inside a panel', () => {
    const fixture = TestBed.createComponent(StatTile);
    fixture.componentRef.setInput('label', 'Cash');
    fixture.componentRef.setInput('value', '$1,200');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.classList).not.toContain('bare');
    fixture.componentRef.setInput('bare', true);
    fixture.detectChanges();
    expect(el.classList).toContain('bare');
  });

  it('marks a phrase as text so it is set quietly in the body face', () => {
    expect(render('Too early to tell').classList).toContain('text');
  });
});
