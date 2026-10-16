import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const mounts = vi.hoisted(() => ({ ws: vi.fn(), layout: vi.fn() }));
vi.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({
    loading: false,
    authEnabled: true,
    isAdmin: false,
    logout: vi.fn(),
    user: { id: 1, permissions: [] },
    hasPermission: () => false,
  }),
}));
// WebSocketProvider in App.tsx is a thin wrapper that calls useWebSocket(),
// so a call to the hook means the provider was mounted.
vi.mock('../../hooks/useWebSocket', () => ({ useWebSocket: () => mounts.ws() }));
vi.mock('../../components/Layout', () => ({
  Layout: () => {
    mounts.layout();
    return null;
  },
}));
vi.mock('react-i18next', async (orig) => ({
  ...(await orig<typeof import('react-i18next')>()),
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en' } }),
}));

import App from '../../App';

describe('training gate in the real route tree', () => {
  it('shows the training page without mounting WebSocketProvider or Layout', () => {
    window.history.pushState({}, '', '/');
    render(<App />);
    expect(screen.getByText('trainingRequired.message')).toBeTruthy();
    expect(mounts.ws).not.toHaveBeenCalled();
    expect(mounts.layout).not.toHaveBeenCalled();
  });
});
