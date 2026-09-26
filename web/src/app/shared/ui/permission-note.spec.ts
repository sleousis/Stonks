import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { provideApi } from '../../api/provide-api';
import type { MeView } from '../../api/models';
import { SessionService } from '../../core/auth/session.service';
import { ADMIN, TRADER } from '../../../testing/auth-fixtures';
import { nextRequest } from '../../../testing/http';
import { PermissionNote } from './permission-note';

@Component({
  imports: [PermissionNote],
  template: `<app-permission-note permission="strategy.promote" />`,
})
class Host {}

describe('PermissionNote', () => {
  async function render(me: MeView) {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), ...provideApi(), provideHttpClientTesting()],
    });
    const controller = TestBed.inject(HttpTestingController);
    const loading = TestBed.inject(SessionService).load();
    (await nextRequest(controller, '/api/auth/me')).flush(me);
    await loading;
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('says why a trader cannot promote', async () => {
    const el = await render(TRADER);
    expect(el.textContent).toContain('Admins only.');
  });

  it('renders nothing for an admin', async () => {
    const el = await render(ADMIN);
    expect(el.querySelector('.permission-note')).toBeNull();
  });
});
