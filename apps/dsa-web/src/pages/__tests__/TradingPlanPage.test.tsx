import { render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import TradingPlanPage from '../TradingPlanPage';

const { getTradingPlan } = vi.hoisted(() => ({ getTradingPlan: vi.fn() }));

vi.mock('../../api/portfolio', () => ({ portfolioApi: { getTradingPlan } }));

beforeEach(() => {
  getTradingPlan.mockResolvedValue({
    generatedAt: '2026-08-31T15:15:00',
    asOf: '2026-08-31',
    totalEquity: 100000,
    investedAmount: 75000,
    cashAmount: 25000,
    exposurePct: 75,
    targetExposurePct: 60,
    cashPct: 25,
    peakEquity: 104000,
    drawdownPct: 3.85,
    riskState: 'normal',
    newPositionsAllowed: false,
    portfolioBlockingReasons: ['当前仓位高于目标'],
    limitations: ['规则化风险管理计划，不构成收益保证或自动交易指令'],
    items: [{
      stockCode: '300475', stockName: '香农芯创', currentQuantity: 100,
      currentPrice: 40, currentWeightPct: 4, positionCapPct: 10,
      volatilityTier: 'high_vol', action: 'observe', actionReason: '接近风险阈值',
      stopPrice: 37, takeProfitLevels: [45, 48], maxAdditionalQuantity: 0,
      blockingReasons: ['组合层面禁止新增仓位'], dataQuality: 'ok',
    }],
  });
});

it('renders a read-only risk plan without order controls', async () => {
  render(<TradingPlanPage />);

  expect(await screen.findByText('香农芯创')).toBeInTheDocument();
  expect(screen.getByText('75.00%')).toBeInTheDocument();
  expect(screen.getByText(/不构成收益保证或自动交易指令/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /买入|卖出|下单/ })).not.toBeInTheDocument();
});
