import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';

import { AuthTokenService } from '../../core/auth/auth-token.service';
import { ToastService } from '../../core/notify/toast.service';
import { type ThemeMode, ThemeService } from '../../core/theme/theme.service';
import { PageHeader } from '../../shared/ui/page-header';

@Component({
  selector: 'app-settings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeader, ReactiveFormsModule],
  templateUrl: './settings.page.html',
  styleUrl: './settings.page.scss',
})
export class SettingsPage {
  private readonly auth = inject(AuthTokenService);
  private readonly toasts = inject(ToastService);
  protected readonly theme = inject(ThemeService);
  private readonly fb = inject(NonNullableFormBuilder);

  protected readonly hasToken = this.auth.hasToken;
  protected readonly showToken = signal(false);
  protected readonly themes: readonly { value: ThemeMode; label: string }[] = [
    { value: 'system', label: 'Match the system' },
    { value: 'light', label: 'Light' },
    { value: 'dark', label: 'Dark' },
  ];

  protected readonly tokenForm = this.fb.group({
    token: ['', [Validators.required, Validators.maxLength(512)]],
  });

  protected readonly readsForm = this.fb.group({
    sendOnReads: [this.auth.sendOnReads()],
  });

  protected saveToken(): void {
    if (this.tokenForm.invalid) {
      this.tokenForm.markAllAsTouched();
      return;
    }
    this.auth.setToken(this.tokenForm.controls.token.value);
    this.tokenForm.reset();
    this.showToken.set(false);
    this.toasts.success('API token saved for this browser tab.', 'Token saved');
  }

  protected clearToken(): void {
    this.auth.clear();
    this.toasts.info('API token removed. The console is read-only until you enter it again.');
  }

  protected toggleReads(): void {
    this.auth.setSendOnReads(this.readsForm.controls.sendOnReads.value);
  }

  protected setTheme(mode: ThemeMode): void {
    this.theme.setMode(mode);
  }
}
