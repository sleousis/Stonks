import { Injectable } from '@angular/core';

import { ROUTE_PERMISSIONS } from '../core/auth/route-permissions.gen';
import { ApiError } from '../core/http/api-error';
import { SILENT_HEADERS } from '../core/http/interceptors';
import { unwrap } from './api-call';
import { client } from './generated/client.gen';

/**
 * Editable operational settings (admins): safe, non-secret switches such as
 * the trading universe, risk caps or job switches, stored by the server with
 * validation and an audit row, TOML staying the base.
 *
 * The route is not in `openapi.json` yet: this is the contract the console
 * builds against, GET and PUT of grouped settings. The form asks only once
 * `PUT /api/admin/settings` is in the generated permissions (`inContract`),
 * and a server that still answers 404 hides it too (`available: false`).
 * When the route lands, run `npm run api:generate` and move these types to
 * the generated client.
 */
export const ADMIN_SETTINGS_URL = '/api/admin/settings';

export type SettingType = 'bool' | 'int' | 'float' | 'percent' | 'string' | 'choice' | 'list';

export interface SettingChoice {
  value: string;
  label: string;
}

export interface SettingField {
  /** Stable id, e.g. `production.risk.max_weight_per_ticker`. Never shown. */
  key: string;
  label: string;
  /** One plain sentence on what it changes. */
  help?: string | null;
  type: SettingType;
  value: unknown;
  /** The TOML base value, shown as "Default". */
  default?: unknown;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  choices?: SettingChoice[] | null;
  /** Shown after number inputs ("days", "%"). */
  unit?: string | null;
  /** The change takes effect after a restart. */
  restart?: boolean | null;
  /** The stored value differs from the TOML base. */
  overridden?: boolean | null;
}

export interface SettingGroup {
  id: string;
  label: string;
  description?: string | null;
  fields: SettingField[];
}

export interface AdminSettingsView {
  groups: SettingGroup[];
  updated_at?: string | null;
  /** Display name of who saved last. */
  updated_by?: string | null;
}

export interface AdminSettingsUpdate {
  /** Only the changed keys. */
  values: Record<string, unknown>;
  /** Kept in the audit log with the change. */
  reason: string;
}

/** What GET answered: the settings, or that this server has no such route yet. */
export type AdminSettingsState =
  { available: true; view: AdminSettingsView } | { available: false };

/** Per-setting messages from a 422, keyed by setting key. */
export function settingErrors(
  err: unknown,
  keys: readonly string[],
): { byKey: Record<string, string>; other: string | null } {
  const byKey: Record<string, string> = {};
  if (!(err instanceof ApiError)) return { byKey, other: err ? String(err) : null };
  const raw = err.problem['errors'];
  const entries: { field: string; message: string }[] = Array.isArray(raw)
    ? raw.map((e: Record<string, unknown>) => ({
        field: String(
          e['key'] ?? e['field'] ?? (Array.isArray(e['loc']) ? e['loc'].join('.') : ''),
        ),
        message: String(e['message'] ?? e['msg'] ?? 'is not valid'),
      }))
    : err.fieldErrors.map((e) => ({ field: e.field, message: e.message }));
  const unmatched: string[] = [];
  for (const e of entries) {
    // FastAPI puts the body path first ("values.<key>"): match on the end.
    const key = keys.find((k) => e.field === k || e.field.endsWith(`.${k}`));
    if (key) byKey[key] = e.message;
    else unmatched.push(e.message);
  }
  const other = unmatched.length
    ? unmatched.join(' ')
    : Object.keys(byKey).length
      ? null
      : err.message;
  return { byKey, other };
}

@Injectable({ providedIn: 'root' })
export class AdminSettingsService {
  /**
   * The feature check: the route is in the API contract the console was
   * built against (`openapi.json`, through the generated permissions). Until
   * it is, nothing is asked for, so no page load ends in a 404.
   */
  inContract(): boolean {
    return `PUT ${ADMIN_SETTINGS_URL}` in ROUTE_PERMISSIONS;
  }

  /** Silent: a 404 only means the server does not offer editable settings yet. */
  async load(): Promise<AdminSettingsState> {
    try {
      const view = await unwrap(
        client.get<{ 200: AdminSettingsView }, unknown, false>({
          security: [{ scheme: 'bearer', type: 'http' }],
          url: ADMIN_SETTINGS_URL,
          headers: SILENT_HEADERS,
        }),
      );
      return { available: true, view: view as AdminSettingsView };
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
        return { available: false };
      }
      throw err;
    }
  }

  /** Saves the changed values; a 422 comes back as an ApiError with per-key errors. */
  async save(update: AdminSettingsUpdate): Promise<AdminSettingsView> {
    const view = await unwrap(
      client.put<{ 200: AdminSettingsView }, unknown, false>({
        security: [{ scheme: 'bearer', type: 'http' }],
        url: ADMIN_SETTINGS_URL,
        body: update,
        headers: { 'Content-Type': 'application/json', ...SILENT_HEADERS },
      }),
    );
    return view as AdminSettingsView;
  }
}
