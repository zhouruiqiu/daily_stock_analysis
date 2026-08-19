import apiClient from './index';
import { toCamelCase } from './utils';

export type LeaderboardItem = { rank?: number | null; strategy: string; completeDays: number; evaluatedCount: number; avgReturnPct: number; avgExcessReturnPct: number; positiveRatePct: number; beatBenchmarkRatePct: number; maxAdverseExcursionPct: number; sampleStatus: string };
export type EvaluationStatus = { enabled: boolean; time: string; topN: number; horizons: number[]; latestRun?: { selectionDate?: string; status?: string } | null };
export type EvaluationRunList = { items: Array<{ runId: string; selectionDate: string; status: string }>; count: number };
export const strategyEvaluationApi = {
  async status() { const { data } = await apiClient.get('/screening/evaluation/status'); return toCamelCase(data) as EvaluationStatus; },
  async leaderboard(horizon: string) { const { data } = await apiClient.get('/screening/evaluation/leaderboard', { params: { horizon, window: 20 } }); return toCamelCase(data) as { horizon: string; items: LeaderboardItem[] }; },
  async runs() { const { data } = await apiClient.get('/screening/evaluation/runs', { params: { limit: 10 } }); return toCamelCase(data) as EvaluationRunList; },
};
