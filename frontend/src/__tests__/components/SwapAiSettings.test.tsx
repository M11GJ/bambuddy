import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SwapAiSettings } from '../../components/SwapAiSettings';

describe('SwapAiSettings', () => {
  it('loads the stored values and saves the edited ones', async () => {
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.get('*/api/v1/settings', () =>
        HttpResponse.json({ swap_ai_mode: 'shadow', swap_ai_url: '', swap_ai_model: 'qwen2.5vl:32b', swap_ai_timeout_s: 60, swap_ai_max_wait_s: 300 }),
      ),
      http.put('*/api/v1/settings', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({});
      }),
    );
    render(<SwapAiSettings />);
    await waitFor(() => expect(screen.getByLabelText('Model')).toHaveValue('qwen2.5vl:32b'));
    await userEvent.selectOptions(screen.getByLabelText('Mode'), 'enforce');
    await userEvent.type(screen.getByLabelText('Ollama URL'), 'http://100.86.155.41:11510');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(bodies).toEqual([
        { swap_ai_mode: 'enforce', swap_ai_url: 'http://100.86.155.41:11510', swap_ai_model: 'qwen2.5vl:32b', swap_ai_timeout_s: 60, swap_ai_max_wait_s: 300 },
      ]),
    );
  });
});
