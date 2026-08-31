import { useEffect, useState } from 'react';
import { strategyEvaluationApi, type EvaluationStatus, type LeaderboardItem } from '../api/strategyEvaluation';

const pct = (value?: number | null) => value == null ? '-' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;

export default function StrategyLeaderboardPage() {
  const [horizon, setHorizon] = useState('5d');
  const [items, setItems] = useState<LeaderboardItem[]>([]);
  const [status, setStatus] = useState<EvaluationStatus | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    Promise.all([strategyEvaluationApi.status(), strategyEvaluationApi.leaderboard(horizon)])
      .then(([nextStatus, board]) => { if (active) { setStatus(nextStatus); setItems(board.items || []); } })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : '加载失败'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [horizon]);
  return <div className="mx-auto max-w-7xl space-y-6 p-4 sm:p-6">
    <section className="rounded-2xl border border-border bg-card p-5">
      <h1 className="text-xl font-semibold">策略排行榜</h1>
      <p className="mt-1 text-sm text-secondary-text">每日 09:25 运行全部策略，以 09:30 开盘价跟踪后续表现。</p>
      <div className="mt-4 flex flex-wrap gap-2">{['1d','3d','5d'].map(value => <button type="button" key={value} onClick={() => { setLoading(true); setError(''); setHorizon(value); }} className={value === horizon ? 'btn-primary' : 'btn-secondary'}>T+{value[0]}</button>)}</div>
      <p className="mt-3 text-xs text-secondary-text">状态：{status?.enabled ? '已启用' : '未启用'} · 最近批次：{status?.latestRun?.selectionDate || '-'}</p>
    </section>
    {error ? <div className="rounded-xl border border-danger/30 bg-danger/10 p-4 text-sm text-danger">{error}</div> : null}
    <section className="overflow-x-auto rounded-2xl border border-border bg-card">
      {loading ? <p className="p-6 text-secondary-text">正在加载...</p> : items.length === 0 ? <p className="p-6 text-secondary-text">尚无选股批次，首批数据将在交易日09:25形成。</p> :
      <table className="min-w-full text-sm"><thead className="border-b border-border text-left text-secondary-text"><tr>{['排名','策略','完整天数','样本','平均收益','超额收益','正收益率','跑赢基准','最大不利波动','状态'].map(x=><th key={x} className="px-4 py-3">{x}</th>)}</tr></thead><tbody>{items.map(item=><tr key={item.strategy} className="border-b border-border/60"><td className="px-4 py-3">{item.rank ?? '-'}</td><td className="px-4 py-3 font-medium">{item.strategy}</td><td className="px-4 py-3">{item.completeDays}</td><td className="px-4 py-3">{item.evaluatedCount}</td><td className="px-4 py-3">{pct(item.avgReturnPct)}</td><td className="px-4 py-3">{pct(item.avgExcessReturnPct)}</td><td className="px-4 py-3">{pct(item.positiveRatePct)}</td><td className="px-4 py-3">{pct(item.beatBenchmarkRatePct)}</td><td className="px-4 py-3">{pct(item.maxAdverseExcursionPct)}</td><td className="px-4 py-3">{item.sampleStatus === 'ranked' ? '已排名' : item.sampleStatus === 'pending' ? '收益评估中' : '样本不足'}</td></tr>)}</tbody></table>}
    </section>
  </div>;
}
