import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import { AuthTokenService } from '../auth/auth-token.service';
import { ToastService } from '../notify/toast.service';
import { SILENT_HEADERS, authInterceptor, errorInterceptor } from './interceptors';

describe('HTTP interceptors', () => {
  let http: HttpClient;
  let controller: HttpTestingController;
  let auth: AuthTokenService;
  let toasts: ToastService;

  beforeEach(() => {
    sessionStorage.clear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([authInterceptor, errorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    http = TestBed.inject(HttpClient);
    controller = TestBed.inject(HttpTestingController);
    auth = TestBed.inject(AuthTokenService);
    toasts = TestBed.inject(ToastService);
  });

  afterEach(() => controller.verify());

  describe('auth', () => {
    it('adds the bearer token to mutating API requests', () => {
      auth.setToken('s3cret');
      http.post('/api/strategies/abc/promote', null).subscribe();
      const req = controller.expectOne('/api/strategies/abc/promote');
      expect(req.request.headers.get('Authorization')).toBe('Bearer s3cret');
      req.flush({});
    });

    it('does not send the token on reads by default', () => {
      auth.setToken('s3cret');
      http.get('/api/portfolio').subscribe();
      const req = controller.expectOne('/api/portfolio');
      expect(req.request.headers.has('Authorization')).toBe(false);
      req.flush({});
    });

    it('sends the token on reads when the trader opts in', () => {
      auth.setToken('s3cret');
      auth.setSendOnReads(true);
      http.get('/api/portfolio').subscribe();
      const req = controller.expectOne('/api/portfolio');
      expect(req.request.headers.get('Authorization')).toBe('Bearer s3cret');
      req.flush({});
    });

    it('sends the token on stream-token calls', () => {
      auth.setToken('s3cret');
      http.post('/api/jobs/j1/stream-token', null).subscribe();
      const req = controller.expectOne('/api/jobs/j1/stream-token');
      expect(req.request.headers.get('Authorization')).toBe('Bearer s3cret');
      req.flush({});
    });

    it('never sends the token to another origin', () => {
      auth.setToken('s3cret');
      http.post('https://example.com/api/x', null).subscribe();
      const req = controller.expectOne('https://example.com/api/x');
      expect(req.request.headers.has('Authorization')).toBe(false);
      req.flush({});
    });

    it('sends nothing without a token', () => {
      http.post('/api/ticks', {}).subscribe();
      const req = controller.expectOne('/api/ticks');
      expect(req.request.headers.has('Authorization')).toBe(false);
      req.flush({});
    });

    it('keeps the token in sessionStorage only', () => {
      auth.setToken('  s3cret  ');
      expect(auth.token()).toBe('s3cret');
      expect(sessionStorage.getItem('stonks.apiToken')).toBe('s3cret');
      expect(JSON.stringify(localStorage)).not.toContain('s3cret');
      auth.clear();
      expect(sessionStorage.getItem('stonks.apiToken')).toBeNull();
    });
  });

  describe('errors', () => {
    const problem = { title: 'Conflict', status: 409, detail: 'strategy is already active' };

    it('toasts the problem detail of a failed mutation and rethrows', async () => {
      const error = vi.spyOn(toasts, 'error');
      const result = firstValueFrom(http.post('/api/strategies/abc/promote', null));
      controller
        .expectOne('/api/strategies/abc/promote')
        .flush(problem, { status: 409, statusText: 'Conflict' });
      await expect(result).rejects.toMatchObject({ status: 409 });
      expect(error).toHaveBeenCalledWith('strategy is already active', 'Conflict');
    });

    it('leaves read failures to the page', async () => {
      const error = vi.spyOn(toasts, 'error');
      const result = firstValueFrom(http.get('/api/portfolio'));
      controller
        .expectOne('/api/portfolio')
        .flush({ title: 'Not Found', status: 404 }, { status: 404, statusText: 'Not Found' });
      await expect(result).rejects.toBeTruthy();
      expect(error).not.toHaveBeenCalled();
    });

    it('toasts auth failures on reads', async () => {
      const error = vi.spyOn(toasts, 'error');
      const result = firstValueFrom(http.get('/api/portfolio'));
      controller
        .expectOne('/api/portfolio')
        .flush(
          { title: 'Unauthorized', status: 401, detail: 'missing or invalid bearer token' },
          { status: 401, statusText: 'Unauthorized' },
        );
      await expect(result).rejects.toBeTruthy();
      expect(error).toHaveBeenCalledWith(
        'missing or invalid bearer token. Sign in, or enter an API token in Settings.',
        'Unauthorized',
      );
    });

    it('stays quiet for silent requests and strips the marker header', async () => {
      const error = vi.spyOn(toasts, 'error');
      const result = firstValueFrom(
        http.post('/api/jobs/j1/stream-token', null, { headers: SILENT_HEADERS }),
      );
      const req = controller.expectOne('/api/jobs/j1/stream-token');
      expect(req.request.headers.has('X-Stonks-Silent')).toBe(false);
      req.flush(problem, { status: 409, statusText: 'Conflict' });
      await expect(result).rejects.toBeTruthy();
      expect(error).not.toHaveBeenCalled();
    });
  });
});
