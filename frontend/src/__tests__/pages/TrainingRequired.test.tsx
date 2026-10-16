import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const auth = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => auth.value }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { ProtectedRoute } from '../../App';
import * as embedded from '../../utils/embedded';

const renderGuard = () =>
  render(
    <MemoryRouter>
      <ProtectedRoute>
        <div>app-content</div>
      </ProtectedRoute>
    </MemoryRouter>,
  );

describe('ProtectedRoute training gate', () => {
  beforeEach(() => {
    auth.value = { loading: false, authEnabled: true, isAdmin: false, logout: vi.fn(), user: { permissions: [] } };
  });

  it('shows the training page and no children for zero permissions', () => {
    renderGuard();
    expect(screen.getByText('trainingRequired.message')).toBeTruthy();
    expect(screen.queryByText('app-content')).toBeNull();
  });

  it('shows logout on direct access and hides it inside the Hub iframe', () => {
    const spy = vi.spyOn(embedded, 'isEmbedded');
    spy.mockReturnValue(false);
    const { unmount } = renderGuard();
    expect(screen.getByText('trainingRequired.logout')).toBeTruthy();
    unmount();
    spy.mockReturnValue(true);
    renderGuard();
    expect(screen.queryByText('trainingRequired.logout')).toBeNull();
    spy.mockRestore();
  });

  it('renders children when the user has permissions', () => {
    auth.value = { ...auth.value, user: { permissions: ['printers:read'] } };
    renderGuard();
    expect(screen.getByText('app-content')).toBeTruthy();
  });

  it('never gates admins with empty permissions', () => {
    auth.value = { ...auth.value, isAdmin: true };
    renderGuard();
    expect(screen.getByText('app-content')).toBeTruthy();
  });

  it('never gates when auth is disabled', () => {
    auth.value = { ...auth.value, authEnabled: false, user: null, isAdmin: true };
    renderGuard();
    expect(screen.getByText('app-content')).toBeTruthy();
  });
});
