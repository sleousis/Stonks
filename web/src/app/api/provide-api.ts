import { provideHttpClient, withFetch, withInterceptors } from '@angular/common/http';
import type { EnvironmentProviders, Provider } from '@angular/core';

import { authInterceptor, errorInterceptor } from '../core/http/interceptors';
import { provideHeyApiClient } from './generated/client/client.gen';
import { client } from './generated/client.gen';

/**
 * HTTP stack (auth + error interceptors) and the generated client bound to
 * Angular's HttpClient. Tests use it too, followed by provideHttpClientTesting().
 */
export function provideApi(): (Provider | EnvironmentProviders)[] {
  return [
    provideHttpClient(withFetch(), withInterceptors([authInterceptor, errorInterceptor])),
    provideHeyApiClient(client),
  ];
}
