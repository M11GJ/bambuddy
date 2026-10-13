/**
 * Printer card SwapMod panel: plate count, hold reason, plates / resume actions.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';

const perms = new Set<string>();
vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return { ...actual, useAuth: () => ({ hasPermission: (p: string) => perms.has(p) }) };
});

import { SwapModPanel, SwapModSettings } from '../../components/SwapModPanel';

const status = (over = {}) => ({ enabled: true, plates_remaining: 3, hold_reason: null, hold_detail: null, config: {}, ...over });

describe('SwapModPanel', () => {
  beforeEach(() => {
    perms.clear();
    perms.add('printers:clear_plate');
  });

  it('renders nothing when swap is disabled', () => {
    render(<SwapModPanel printerId={1} swap={{ enabled: false, plates_remaining: 3, hold_reason: 'swap.gate.noPlate' }} />);
    expect(screen.queryByText('Plate swapper')).not.toBeInTheDocument();
    expect(screen.queryByText('No plate detected on the bed')).not.toBeInTheDocument();
  });

  it('shows the plate count and the translated hold reason', () => {
    render(<SwapModPanel printerId={1} swap={{ enabled: true, plates_remaining: 0, hold_reason: 'swap.hold.outOfPlates' }} />);
    expect(screen.getByText('Spare plates: 0')).toBeInTheDocument();
    expect(screen.getByText('Out of spare plates — refill and enter the count')).toBeInTheDocument();
  });

  it('shows "not entered" when the count is unknown', () => {
    render(<SwapModPanel printerId={1} swap={{ enabled: true, plates_remaining: null, hold_reason: null }} />);
    expect(screen.getByText('Spare plates: not entered')).toBeInTheDocument();
  });

  it('hides the plate and resume actions without printers:clear_plate', () => {
    perms.clear();
    render(<SwapModPanel printerId={1} swap={{ enabled: true, plates_remaining: 1, hold_reason: 'swap.gate.noPlate' }} />);
    expect(screen.getByText('No plate detected on the bed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Enter plate count' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Checked — resume' })).not.toBeInTheDocument();
  });

  it('only offers resume while held', () => {
    render(<SwapModPanel printerId={1} swap={{ enabled: true, plates_remaining: 1, hold_reason: null }} />);
    expect(screen.getByRole('button', { name: 'Enter plate count' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Checked — resume' })).not.toBeInTheDocument();
  });

  it('posts resume', async () => {
    const calls: string[] = [];
    server.use(
      http.post('*/api/v1/printers/1/swap/resume', () => {
        calls.push('resume');
        return HttpResponse.json(status());
      }),
    );
    render(<SwapModPanel printerId={1} swap={{ enabled: true, plates_remaining: 1, hold_reason: 'swap.hold.printNotConfirmed' }} />);
    await userEvent.click(screen.getByRole('button', { name: 'Checked — resume' }));
    await waitFor(() => expect(calls).toEqual(['resume']));
  });

  it('only sends plate counts in 0..99', async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post('*/api/v1/printers/1/swap/plates', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(status({ plates_remaining: 5 }));
      }),
    );
    render(<SwapModPanel printerId={1} swap={{ enabled: true, plates_remaining: null, hold_reason: 'swap.hold.platesUnknown' }} />);
    const input = screen.getByRole('spinbutton');
    const button = screen.getByRole('button', { name: 'Enter plate count' });

    await userEvent.type(input, '100');
    expect(button).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, '-1');
    expect(button).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, '2.5');
    expect(button).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, '5');
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(bodies).toEqual([{ remaining: 5 }]));
  });
});

describe('SwapModSettings', () => {
  it('saves enable + config and shows the backend 422 message', async () => {
    const bodies: unknown[] = [];
    const config = { marker: 'M', exclude_front_mm: 20, exclude_back_mm: 10, bed_depth_mm: 256, clip_roi: [0.3, 0.7, 0.15, 0.3], clip_min_percent: 4 };
    server.use(
      http.get('*/api/v1/printers/1/swap', () => HttpResponse.json(status({ enabled: false, config }))),
      http.put('*/api/v1/printers/1/swap', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ detail: 'swap_config exclusion zones leave no printable depth' }, { status: 422 });
      }),
    );
    render(<SwapModSettings printerId={1} />);
    await userEvent.click(await screen.findByLabelText('Enable plate swapper'));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('swap_config exclusion zones leave no printable depth')).toBeInTheDocument();
    expect(bodies).toEqual([{ enabled: true, config }]);
  });
});
