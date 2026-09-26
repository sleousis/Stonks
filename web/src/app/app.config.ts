import {
  type ApplicationConfig,
  Injectable,
  inject,
  provideAppInitializer,
  provideBrowserGlobalErrorListeners,
} from '@angular/core';
import { Title } from '@angular/platform-browser';
import {
  type RouterStateSnapshot,
  TitleStrategy,
  provideRouter,
  withComponentInputBinding,
  withInMemoryScrolling,
} from '@angular/router';

import { provideApi } from './api/provide-api';
import { routes } from './app.routes';
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
      routes,
      withComponentInputBinding(),
      withInMemoryScrolling({ scrollPositionRestoration: 'top' }),
    ),
    { provide: TitleStrategy, useClass: StonksTitleStrategy },
    ...provideApi(),
    // Apply the stored locale / time zone before the first page renders.
    provideAppInitializer(() => void inject(FormatService)),
  ],
};
