/** Users list shows the member's real name next to the login ID. */

import { describe, it, expect, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { UsersPage } from '../../pages/UsersPage';
import { setAuthToken } from '../../api/client';

afterEach(() => setAuthToken(null));

describe('UsersPage display name', () => {
  it('renders "display (username)" when set, username otherwise', async () => {
    setAuthToken('test-token', 'session');
    const base = { role: 'user', is_active: true, is_admin: false, groups: [], permissions: [], created_at: '2026-01-01' };
    server.use(
      http.get('*/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: true, requires_setup: false })),
      http.get('*/api/v1/auth/me', () =>
        HttpResponse.json({ id: 1, username: 'admin', is_admin: true, permissions: ['users:read', 'users:create', 'users:update', 'users:delete'] }),
      ),
      http.get('*/api/v1/users/', () =>
        HttpResponse.json([
          { ...base, id: 2, username: 'yamada_t', display_name: '山田太郎' },
          { ...base, id: 3, username: 'plainuser', display_name: null },
        ]),
      ),
    );

    render(<UsersPage />);

    expect(await screen.findByText('山田太郎 (yamada_t)')).toBeInTheDocument();
    expect(screen.getByText('plainuser')).toBeInTheDocument();
  });
});
