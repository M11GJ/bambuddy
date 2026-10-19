/**
 * The enqueue-time swap guard answers 422 "[swap.guard.noMarker] English ...";
 * the member must see it in their language, not the raw key + English.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import i18n from '../../i18n';
import ja from '../../i18n/locales/ja';

const mockShowToast = vi.fn();
vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: mockShowToast }) };
});

import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';

describe('PrintModal swap guard error', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage('ja');
    server.use(
      http.get('/api/v1/printers/', () =>
        HttpResponse.json([{ id: 1, name: 'A1 Swap', model: 'A1', ip_address: '192.168.1.100', enabled: true, is_active: true }]),
      ),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
      ),
      http.post('/api/v1/queue/', () =>
        HttpResponse.json(
          { detail: '[swap.guard.noMarker] Print file rejected for the swap printer' },
          { status: 422 },
        ),
      ),
    );
  });

  afterEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('shows the Japanese noMarker text', async () => {
    const user = userEvent.setup();
    render(<PrintModal mode="create" archiveId={1} archiveName="Benchy" initialSelectedPrinterIds={[1]} onClose={vi.fn()} />);
    const buttons = await screen.findAllByRole('button', { name: /print|印刷|プリント/i });
    await user.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(mockShowToast).toHaveBeenCalled());
    const msg = mockShowToast.mock.calls.map((c) => c[0]).join('\n');
    expect(msg).toContain((ja as any).swap.guard.noMarker);
    expect(msg).not.toContain('[swap.guard.noMarker]');
  });
});
