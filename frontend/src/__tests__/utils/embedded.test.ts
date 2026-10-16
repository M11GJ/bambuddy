import { describe, it, expect, vi, afterEach } from 'vitest';
import { isEmbedded } from '../../utils/embedded';

describe('isEmbedded', () => {
  afterEach(() => vi.restoreAllMocks());
  it('false at top level', () => expect(isEmbedded()).toBe(false));
  it('true in a frame', () => {
    vi.spyOn(window, 'top', 'get').mockReturnValue({} as Window);
    expect(isEmbedded()).toBe(true);
  });
  it('true when top access throws', () => {
    vi.spyOn(window, 'top', 'get').mockImplementation(() => {
      throw new Error('cross-origin');
    });
    expect(isEmbedded()).toBe(true);
  });
});
