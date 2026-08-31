import { useEffect, useState } from 'react';
import { portfolioApi } from '../api/portfolio';
import type { PortfolioTradingPlanResponse } from '../types/portfolio';

const money = (value?: number | null) => value == null ? '-' : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const pct = (value?: number | null) => value == null ? '-' : `${value.toFixed(2)}%`;
const actionLabel: Record<string, string> = {
  hold: '持有', observe: '观察', reduce: '建议减仓', exit: '建议退出',
  add_if_confirmed: '条件确认后可加仓', blocked: '已阻止',
};
const riskLabel: Record<string, string> = {
  normal: '正常', caution: '警惕', defensive: '防守', drawdown_lock: '回撤锁定',
};

export default function TradingPlanPage() {
  const [plan, setPlan] = useState<PortfolioTradingPlanResponse | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    portfolioApi.getTradingPlan()
      .then(value => { if (active) setPlan(value); })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '交易计划加载失败'); });
    return () => { active = false; };
  }, []);

  if (error) return <div className="m-6 rounded-xl border border-danger/30 bg-danger/10 p-5 text-danger">{error}</div>;
  if (!plan) return <p className="p-6 text-secondary-text">正在生成交易计划...</p>;

  const metrics = [
    ['净资产', money(plan.totalEquity)], ['当前仓位', pct(plan.exposurePct)],
    ['目标仓位', pct(plan.targetExposurePct)], ['现金比例', pct(plan.cashPct)],
    ['净值高点', money(plan.peakEquity)], ['当前回撤', pct(plan.drawdownPct)],
  ];
  return <div className="mx-auto max-w-7xl space-y-6 p-4 sm:p-6">
    <section className="rounded-2xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="text-xl font-semibold">组合交易计划</h1><p className="mt-1 text-sm text-secondary-text">只读风险管理建议，不连接券商、不提供下单入口。</p></div>
        <span className="rounded-full border border-border px-3 py-1 text-sm">风险状态：{riskLabel[plan.riskState] || plan.riskState}</span>
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{metrics.map(([label, value]) => <div key={label} className="rounded-xl bg-base p-4"><p className="text-xs text-secondary-text">{label}</p><p className="mt-1 text-lg font-semibold">{value}</p></div>)}</div>
      <p className="mt-4 text-xs text-secondary-text">数据日期：{plan.asOf} · 生成时间：{new Date(plan.generatedAt).toLocaleString('zh-CN')}</p>
    </section>

    {plan.portfolioBlockingReasons.length ? <section className="rounded-2xl border border-warning/30 bg-warning/10 p-5"><h2 className="font-semibold">组合约束</h2><ul className="mt-2 list-disc space-y-1 pl-5 text-sm">{plan.portfolioBlockingReasons.map(item => <li key={item}>{item}</li>)}</ul></section> : null}

    <section className="overflow-x-auto rounded-2xl border border-border bg-card">
      <table className="min-w-full text-sm"><thead className="border-b border-border text-left text-secondary-text"><tr>{['持仓','仓位/上限','动作','理由','止损/止盈','建议数量','阻止原因'].map(label => <th key={label} className="px-4 py-3">{label}</th>)}</tr></thead>
        <tbody>{plan.items.map(item => <tr key={item.stockCode} className="border-b border-border/60 align-top"><td className="px-4 py-3"><p className="font-medium">{item.stockName}</p><p className="text-xs text-secondary-text">{item.stockCode} · {item.currentQuantity}股 @ {item.currentPrice}</p></td><td className="px-4 py-3">{pct(item.currentWeightPct)} / {pct(item.positionCapPct)}</td><td className="px-4 py-3 font-medium">{actionLabel[item.action] || item.action}</td><td className="max-w-sm px-4 py-3">{item.actionReason}</td><td className="px-4 py-3"><p>止损 {item.stopPrice ?? '-'}</p><p className="text-xs text-secondary-text">止盈 {item.takeProfitLevels.length ? item.takeProfitLevels.join(' / ') : '-'}</p></td><td className="px-4 py-3"><p>最多减 {item.maxReduceQuantity ?? 0}股</p><p className="text-xs text-secondary-text">条件加 {item.maxAdditionalQuantity ?? 0}股</p></td><td className="max-w-xs px-4 py-3">{item.blockingReasons.length ? item.blockingReasons.join('；') : '-'}</td></tr>)}</tbody>
      </table>
      {!plan.items.length ? <p className="p-6 text-secondary-text">当前没有持仓计划。</p> : null}
    </section>
    <section className="rounded-xl border border-border bg-card p-4 text-sm text-secondary-text">{plan.limitations.map(item => <p key={item}>{item}</p>)}</section>
  </div>;
}
