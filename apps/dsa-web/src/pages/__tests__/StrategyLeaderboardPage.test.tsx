import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import StrategyLeaderboardPage from '../StrategyLeaderboardPage';

const { status, leaderboard } = vi.hoisted(() => ({
  status: vi.fn(),
  leaderboard: vi.fn(),
}));

vi.mock('../../api/strategyEvaluation', () => ({
  strategyEvaluationApi: { status, leaderboard },
}));

beforeEach(() => {
  vi.clearAllMocks();
  status.mockResolvedValue({ enabled: true, latestRun: { selectionDate: '2026-08-31' } });
  leaderboard.mockResolvedValue({ items: [] });
});

it('loads separate cohort boards and switches to opening board', async () => {
  render(<StrategyLeaderboardPage />);

  await waitFor(() => expect(leaderboard).toHaveBeenCalledWith('5d', 'preopen_previous_close'));
  fireEvent.click(screen.getByRole('button', { name: '开盘策略榜' }));

  await waitFor(() => expect(leaderboard).toHaveBeenLastCalledWith('5d', 'opening_0945'));
  expect(screen.getByText(/09:45/)).toBeInTheDocument();
});
