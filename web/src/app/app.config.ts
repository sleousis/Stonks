import {
  type ApplicationConfig,
  Injectable,
  inject,
  isDevMode,
  provideAppInitializer,
  provideBrowserGlobalErrorListeners,
} from '@angular/core';
import { Title } from '@angular/platform-browser';
import { provideServiceWorker } from '@angular/service-worker';
import {
  type RouterStateSnapshot,
  TitleStrategy,
  provideRouter,
  withComponentInputBinding,
  withInMemoryScrolling,
} from '@angular/router';

import { provideApi } from './api/provide-api';
import { routes } from './app.routes';
import { protectRoutes } from './core/auth/auth.guards';
import { FormatService } from './core/format/format.service';

/** "Dashboard – Stonks" */
@Injectable({ providedIn: 'root' })
export class StonksTitleStrategy extends TitleStrategy {
  private readonly title = inject(Title);

  override updateTitle(snapshot: RouterStateSnapshot): void {
    const page = this.buildTitle(snapshot);
    this.title.setTitle(page ? `${page} – Stonks` : 'Stonks');
  }
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(
      protectRoutes(routes),
      withComponentInputBinding(),
      withInMemoryScrolling({ scrollPositionRestoration: 'top' }),
    ),
    { provide: TitleStrategy, useClass: StonksTitleStrategy },
    ...provideApi(),
    // Apply the stored locale / time zone before the first page renders.
    provideAppInitializer(() => void inject(FormatService)),
    // Caches the app shell only (ngsw-config.json has no data groups: API
    // responses are never cached). Also the Web Push endpoint for SwPush.
    provideServiceWorker('ngsw-worker.js', {
      enabled: !isDevMode(),
      registrationStrategy: 'registerWhenStable:30000',
    }),
  ],
};
