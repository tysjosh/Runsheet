/**
 * Unit tests for the decision logic in the four new components added in task
 * 18.9: the signature data-URL unwrap (R5.1), the cross-platform prompt's
 * validation (R16.15), the queue chip's visibility and label (R11.10), and the
 * permission banner's notice selection (R9.12, R10.15).
 *
 * Each of these is the part of its component that decides something, so it is
 * tested directly rather than through a render.
 *
 * `react-native-webview` is stubbed because `react-native-signature-canvas`
 * imports it at module load and it resolves a native TurboModule
 * (`RNCWebViewModule`) that no JS test environment provides. The stub is a
 * module-load concern only — nothing in this file renders the canvas.
 */

jest.mock('react-native-webview', () => ({
  __esModule: true,
  WebView: 'WebView',
  default: 'WebView',
}));

import {
  parseSignatureDataUrl,
  SIGNATURE_ARTIFACT_CATEGORY,
  SIGNATURE_MIME_TYPE,
} from '@/components/SignaturePad';
import { validatePromptValue } from '@/components/PromptDialog';
import {
  outstandingQueueCount,
  queueChipLabel,
  queueChipVariant,
  shouldShowQueueChip,
} from '@/components/PendingQueueChip';
import { deniedPermissionNotices } from '@/components/PermissionBanner';

describe('parseSignatureDataUrl', () => {
  it('unwraps a PNG data URL into base64 bytes tagged for the signature category', () => {
    expect(parseSignatureDataUrl('data:image/png;base64,QUJD')).toEqual({
      base64: 'QUJD',
      mimeType: SIGNATURE_MIME_TYPE,
      category: SIGNATURE_ARTIFACT_CATEGORY,
    });
  });

  it('strips the newlines a webview payload can carry', () => {
    expect(parseSignatureDataUrl('data:image/png;base64,QU\nJD\n')?.base64).toBe('QUJD');
  });

  it('accepts a bare base64 payload when the prefix is omitted', () => {
    expect(parseSignatureDataUrl('QUJD')?.base64).toBe('QUJD');
  });

  it('rejects a data URL declaring a format other than PNG', () => {
    expect(parseSignatureDataUrl('data:image/jpeg;base64,QUJD')).toBeNull();
  });

  it('rejects an empty or absent signature', () => {
    expect(parseSignatureDataUrl('')).toBeNull();
    expect(parseSignatureDataUrl('   ')).toBeNull();
    expect(parseSignatureDataUrl(null)).toBeNull();
    expect(parseSignatureDataUrl(undefined)).toBeNull();
  });
});

describe('validatePromptValue', () => {
  it('trims and returns a text answer', () => {
    expect(validatePromptValue('  spill at the fill port ')).toEqual({
      ok: true,
      value: 'spill at the fill port',
    });
  });

  it('rejects an empty answer when the prompt is required', () => {
    const result = validatePromptValue('   ');
    expect(result.ok).toBe(false);
  });

  it('allows an empty answer when the prompt is optional', () => {
    expect(validatePromptValue('   ', { required: false })).toEqual({ ok: true, value: '' });
  });

  it('parses a numeric answer to a number', () => {
    expect(validatePromptValue('4200.5', { mode: 'number' })).toEqual({
      ok: true,
      value: 4200.5,
    });
  });

  it('rejects a non-numeric answer in numeric mode', () => {
    expect(validatePromptValue('forty two', { mode: 'number' }).ok).toBe(false);
  });

  it('enforces the inclusive bounds in numeric mode', () => {
    expect(validatePromptValue('0', { mode: 'number', min: 1 }).ok).toBe(false);
    expect(validatePromptValue('1', { mode: 'number', min: 1 }).ok).toBe(true);
    expect(validatePromptValue('11', { mode: 'number', max: 10 }).ok).toBe(false);
    expect(validatePromptValue('10', { mode: 'number', max: 10 }).ok).toBe(true);
  });

  it('enforces a text length limit', () => {
    expect(validatePromptValue('abcdef', { maxLength: 5 }).ok).toBe(false);
  });
});

describe('PendingQueueChip logic', () => {
  it('counts pending and failed rows together', () => {
    expect(outstandingQueueCount({ pending: 3, failed: 2 })).toBe(5);
  });

  it('ignores a negative or non-finite count rather than rendering it', () => {
    expect(outstandingQueueCount({ pending: -1, failed: Number.NaN })).toBe(0);
  });

  it('shows while offline even with an empty queue (R11.10)', () => {
    expect(shouldShowQueueChip({ pending: 0, failed: 0 }, false)).toBe(true);
  });

  it('shows online only when something is outstanding', () => {
    expect(shouldShowQueueChip({ pending: 0, failed: 0 }, true)).toBe(false);
    expect(shouldShowQueueChip({ pending: 1, failed: 0 }, true)).toBe(true);
    expect(shouldShowQueueChip({ pending: 0, failed: 1 }, true)).toBe(true);
  });

  it('labels the outstanding count, naming failures separately', () => {
    expect(queueChipLabel({ pending: 2, failed: 0 }, true)).toBe('2 queued');
    expect(queueChipLabel({ pending: 2, failed: 1 }, false)).toBe('Offline · 3 queued · 1 failed');
    expect(queueChipLabel({ pending: 0, failed: 0 }, false)).toBe('Offline · nothing queued');
  });

  it('grades a failure above the offline state', () => {
    expect(queueChipVariant({ pending: 1, failed: 1 }, false)).toBe('destructive');
    expect(queueChipVariant({ pending: 1, failed: 0 }, false)).toBe('secondary');
    expect(queueChipVariant({ pending: 1, failed: 0 }, true)).toBe('default');
  });
});

describe('deniedPermissionNotices', () => {
  it('raises a notice for each denied permission, notifications first', () => {
    const notices = deniedPermissionNotices({ notifications: 'denied', location: 'denied' });
    expect(notices.map((notice) => notice.id)).toEqual(['notifications', 'location']);
  });

  it('raises nothing for a granted or undetermined permission', () => {
    expect(deniedPermissionNotices({ notifications: 'granted', location: 'undetermined' })).toEqual(
      [],
    );
    expect(deniedPermissionNotices({})).toEqual([]);
  });

  it('tells the driver that status changes and POD still work without location (R10.15)', () => {
    const [notice] = deniedPermissionNotices({ location: 'denied' });
    expect(notice.description).toMatch(/proof of delivery/i);
  });

  it('offers a route to the device notification settings (R9.12)', () => {
    const [notice] = deniedPermissionNotices({ notifications: 'denied' });
    expect(notice.actionLabel).toMatch(/notification settings/i);
  });
});
