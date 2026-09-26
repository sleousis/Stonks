import { provideHttpClient, withFetch, withInterceptors } from '@angular/common/http';
import type { EnvironmentProviders, Provider } from '@angular/core';

import { sessionInterceptor } from '../core/auth/session.interceptor';
import { authInterceptor, errorInterceptor } from '../core/http/interceptors';
import { provideHeyApiClient } from './generated/client/client.gen';
import { client } from './generated/client.gen';

/**
 * HTTP stack (token, error and session interceptors) and the generated
 * client bound to Angular's HttpClient. The session interceptor comes last
 * so it sees failures before the error interceptor toasts them (a step-up
 * that succeeds is retried, never toasted). Tests use it too, followed by
 * provideHttpClientTesting().
 */
export function provideApi(): (Provider | EnvironmentProviders)[] {
  return [
    provideHttpClient(
      withFetch(),
      withInterceptors([authInterceptor, errorInterceptor, sessionInterceptor]),
    ),
    provideHeyApiClient(client),
  ];
}
