import { useEffect, useMemo, useState } from "react";
import Chart from "./components/Chart";
import { useDashboard } from "./hooks/useDashboard";
import { formatMoney, formatNumber, signed, trendClass } from "./lib/format";

function useCompactLayout() {
  const [compact, setCompact] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia("(max-width: 760px)").matches : false,
  );

  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const onChange = () => setCompact(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  return compact;
}

const SERIES_COLORS = [
  "#e34545",
  "#d97a27",
  "#bd9a26",
  "#7a9e35",
  "#20a574",
  "#1999a6",
  "#3587c7",
  "#5e6ec5",
  "#7d55be",
  "#a34da9",
  "#c04f7a",
  "#7b8795",
  "#9d704e",
  "#4e9b90",
  "#c45c26",
  "#6b8f3c",
  "#2a7f9e",
  "#8a5a9c",
  "#b85c6e",
  "#5c7a8a",
  "#a67c52",
  "#3d8f7a",
  "#c4783a",
  "#5a6fa5",
  "#9c6b3c",
  "#4a9a6a",
  "#7a5c8f",
  "#b06a4a",
  "#3c7a8f",
  "#8f6a3c",
  "#5c8f6a",
  "#8a4a6a",
  "#6a7a4a",
  "#4a6a8f",
  "#9a5a4a",
  "#4a8f8a",
  "#7a6a9a",
  "#8f7a4a",
  "#5a8f4a",
  "#6a4a8f",
];

function formatSectorTooltip(sector, pointValue) {
  if (!sector) return "";
  const leaders = Array.isArray(sector.leaders) ? sector.leaders.slice(0, 15) : [];
  const leaderRows = leaders.length
    ? leaders
        .map((stock, index) => {
          const tone = Number(stock.changePct) > 0 ? "up" : Number(stock.changePct) < 0 ? "down" : "flat";
          return `<div class="sector-tip-row">
            <span class="sector-tip-rank">${index + 1}</span>
            <span class="sector-tip-name">${stock.name}</span>
            <span class="sector-tip-code">${stock.code}</span>
            <span class="sector-tip-chg ${tone}">${signed(stock.changePct, 2)}%</span>
          </div>`;
        })
        .join("")
    : `<div class="sector-tip-empty">暂无代表票</div>`;

  const pointText =
    pointValue == null || Number.isNaN(Number(pointValue))
      ? "--"
      : `${signed(pointValue, 2)}%`;

  return `<div class="sector-tip">
    <div class="sector-tip-head">
      <strong>${sector.name}</strong>
      <span class="${trendClass(sector.changePct)}">${signed(sector.changePct, 2)}%</span>
    </div>
    <div class="sector-tip-meta">${sector.type || "板块"} · 此刻 ${pointText} · 领涨前 ${Math.min(15, leaders.length)} 只</div>
    <div class="sector-tip-list">${leaderRows}</div>
  </div>`;
}

function pickSectorTooltipTarget(params, byName) {
  const items = Array.isArray(params) ? params : [params];
  const usable = items.filter(
    (item) => item && item.seriesName && byName.has(item.seriesName) && item.value != null,
  );
  if (!usable.length) return null;
  if (usable.length === 1) return usable[0];
  return usable.reduce((best, item) =>
    Math.abs(Number(item.value)) > Math.abs(Number(best.value)) ? item : best,
  );
}

function buildSectorOption(sectors, { compact = false } = {}) {
  const longest = sectors.reduce(
    (result, sector) => (sector.series.length > result.length ? sector.series : result),
    [],
  );
  const times = longest.map((point) => point.time);
  const maxChange = Math.max(...sectors.map((item) => item.changePct), 0);
  const byName = new Map(sectors.map((sector) => [sector.name, sector]));
  const aligned = sectors.map((sector) => {
    const values = new Map(sector.series.map((point) => [point.time, point.value]));
    return {
      name: sector.name,
      type: "line",
      data: times.map((time) => values.get(time) ?? null),
      showSymbol: true,
      showAllSymbol: true,
      symbol: "circle",
      symbolSize: compact ? 10 : 8,
      itemStyle: { opacity: 0 },
      connectNulls: true,
      triggerLineEvent: true,
      animationDurationUpdate: 350,
      lineStyle: { width: sector.changePct === maxChange ? 2.4 : compact ? 1.8 : 1.6 },
      emphasis: {
        focus: "series",
        blurScope: "coordinateSystem",
        itemStyle: { opacity: 1, borderWidth: 1.5, borderColor: "#fff" },
        lineStyle: { width: 3.2 },
      },
    };
  });

  return {
    color: SERIES_COLORS,
    animation: false,
    aria: { enabled: true, description: "行业与概念板块的日内涨跌幅曲线" },
    tooltip: {
      trigger: "item",
      triggerOn: "mousemove|click",
      enterable: true,
      appendToBody: true,
      confine: false,
      hideDelay: 120,
      extraCssText:
        "z-index:40;max-width:min(320px,calc(100vw - 24px));padding:0;border:none;box-shadow:none;background:transparent;",
      formatter: (params) => {
        const target = pickSectorTooltipTarget(params, byName);
        if (!target) return "";
        return formatSectorTooltip(byName.get(target.seriesName), target.value);
      },
    },
    legend: { show: false },
    grid: {
      left: compact ? 40 : 52,
      right: compact ? 12 : 18,
      top: 16,
      bottom: compact ? 28 : 34,
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: times,
      axisLine: { lineStyle: { color: "#dfe5ec" } },
      axisTick: { show: false },
      axisLabel: {
        color: "#8a95a3",
        fontSize: compact ? 10 : 12,
        interval: Math.max(1, Math.floor(times.length / (compact ? 3 : 4))) - 1,
      },
    },
    yAxis: {
      type: "value",
      scale: true,
      axisLabel: { color: "#8a95a3", fontSize: compact ? 10 : 12, formatter: "{value}%" },
      splitLine: { lineStyle: { color: "#edf0f4" } },
    },
    series: aligned,
  };
}

function SectorLegend({ sectors }) {
  if (!sectors.length) return null;
  return (
    <ul className="sector-legend" aria-label="板块列表">
      {sectors.map((sector, index) => (
        <li key={sector.code || sector.name} className="sector-legend-item">
          <span
            className="sector-legend-swatch"
            style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }}
          />
          <span className="sector-legend-name">{sector.name}</span>
          <span className={`sector-legend-chg ${trendClass(sector.changePct)}`}>
            {signed(sector.changePct, 2)}%
          </span>
        </li>
      ))}
    </ul>
  );
}

function buildIndexOption(index) {
  const values = index.series || [];
  const rising = index.changePct >= 0;
  const color = rising ? "#d9363e" : "#15935b";
  return {
    animation: false,
    aria: { enabled: true, description: `${index.name}日内分时走势` },
    tooltip: { trigger: "axis", confine: true },
    grid: { left: 0, right: 0, top: 8, bottom: 0 },
    xAxis: { type: "category", data: values.map((point) => point.time), show: false },
    yAxis: { type: "value", scale: true, show: false },
    series: [
      {
        type: "line",
        data: values.map((point) => point.value),
        showSymbol: false,
        lineStyle: { color, width: 1.6 },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: rising ? "rgba(217,54,62,.20)" : "rgba(21,147,91,.20)" },
              { offset: 1, color: "rgba(255,255,255,0)" },
            ],
          },
        },
      },
    ],
  };
}

function buildBreadthOption(distribution) {
  return {
    animationDurationUpdate: 300,
    aria: { enabled: true, description: "全市场个股涨跌幅分布" },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, confine: true },
    grid: { left: 34, right: 8, top: 28, bottom: 30 },
    xAxis: {
      type: "category",
      data: distribution.map((item) => item.label),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: "#dfe5ec" } },
      axisLabel: { color: "#8a95a3", fontSize: 9, interval: 0, rotate: 35 },
    },
    yAxis: { type: "value", show: false },
    series: [
      {
        type: "bar",
        barMaxWidth: 26,
        data: distribution.map((item, index) => ({
          value: item.count,
          itemStyle: {
            color: index < 6 ? "#16935b" : index === 6 ? "#9ba5b1" : "#d9363e",
            borderRadius: [3, 3, 0, 0],
          },
        })),
        label: { show: true, position: "top", color: "#637080", fontSize: 9 },
      },
    ],
  };
}

function ToolbarActions({ refreshing, onRefresh }) {
  return (
    <div className="toolbar-actions">
      <button type="button" className="refresh-button" onClick={onRefresh} disabled={refreshing}>
        <span className={refreshing ? "refresh-icon spinning" : "refresh-icon"}>↻</span>
        {refreshing ? "刷新中" : "刷新"}
      </button>
    </div>
  );
}

function LoadingView() {
  return (
    <main className="loading-view" aria-live="polite">
      <div className="loading-spinner" />
      <p>正在汇总沪深行情…</p>
    </main>
  );
}

function App() {
  // Keep one sector family on the main plot so the intraday lines remain readable.
  const [sectorFilter, setSectorFilter] = useState("行业");
  const compact = useCompactLayout();
  const { data, loading, refreshing, error, refresh } = useDashboard();

  const sectors = useMemo(() => {
    if (!data) return [];
    return data.sectors.filter((sector) => sector.type === sectorFilter);
  }, [data, sectorFilter]);

  const sectorOption = useMemo(
    () => buildSectorOption(sectors, { compact }),
    [sectors, compact],
  );
  const breadthOption = useMemo(
    () => buildBreadthOption(data?.breadth.distribution || []),
    [data],
  );

  if (loading && !data) return <LoadingView />;

  if (!data) {
    return (
      <main className="loading-view">
        <p className="error-text">{error || "暂时无法读取行情"}</p>
        <button type="button" className="primary-button" onClick={refresh}>重新加载</button>
      </main>
    );
  }

  const { meta, indices, breadth, turnover, limitPool } = data;
  const updatedAt = new Date(meta.updatedAt).toLocaleTimeString("zh-CN", { hour12: false });
  const downRatio = breadth.down / Math.max(1, breadth.up + breadth.flat + breadth.down);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <div className="eyebrow">A-SHARE MARKET PULSE</div>
          <div className="title-row">
            <h1>A 股全景</h1>
            <span className={`market-status ${meta.isTrading ? "open" : "closed"}`}>
              <span />{meta.isTrading ? "交易中" : "已休市"}
            </span>
          </div>
          <p>指数、行业与概念板块的实时市场宽度</p>
        </div>
        <ToolbarActions refreshing={refreshing} onRefresh={refresh} />
      </header>

      <div className="source-strip" role="status">
        <span className={`source-dot ${meta.stale ? "stale" : "live"}`} />
        <strong>{meta.source}</strong>
        <span>{meta.message}</span>
        {meta.sectorSource ? <span>{meta.sectorSource}</span> : null}
        {meta.stockCount ? <span>覆盖 {meta.stockCount} 只股票 · {meta.sectorCount} 个板块</span> : null}
        <time>更新 {updatedAt}</time>
      </div>
      {error && data ? <div className="error-banner">{error}，当前继续展示最近一次成功数据。</div> : null}

      <section className="index-row" aria-label="主要指数">
        {indices.map((index) => (
          <article className="index-card" key={index.code}>
            <div className="index-card-copy">
              <div className="index-name">{index.name}</div>
              <div className="index-price">{formatNumber(index.price, 2)}</div>
              <div className={trendClass(index.changePct)}>
                {signed(index.changePct, 2)}% <small>{signed(index.change, 2)}</small>
              </div>
            </div>
            <Chart option={buildIndexOption(index)} className="index-chart" ariaLabel={`${index.name}分时图`} />
            <div className="index-range">
              <span>高 {formatNumber(index.high, 2)}</span>
              <span>低 {formatNumber(index.low, 2)}</span>
            </div>
          </article>
        ))}
      </section>

      <main className="dashboard-grid">
        <section className="panel dominant-panel">
          <div className="panel-heading">
            <div>
              <span className="panel-kicker">MARKET ROTATION</span>
              <h2>涨跌板块分时对比</h2>
            </div>
            <div className="segmented small" aria-label="板块类型">
              {["行业", "概念"].map((item) => (
                <button
                  type="button"
                  key={item}
                  className={sectorFilter === item ? "active" : ""}
                  aria-pressed={sectorFilter === item}
                  onClick={() => setSectorFilter(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>
          <div className="sector-board">
            <Chart option={sectorOption} className="sector-chart" ariaLabel="热门行业和概念板块日内涨跌幅曲线" />
            <SectorLegend sectors={sectors} />
          </div>
          <div className="chart-note">
            曲线为相对昨收涨跌幅；电脑悬停或手机点按曲线，可查看该板块涨幅前 15 只代表票。下方分两列展示当前类别板块。
          </div>
        </section>

        <aside className="side-stack">
          <section className="panel turnover-panel">
            <div className="panel-heading compact">
              <div>
                <span className="panel-kicker">TURNOVER</span>
                <h2>沪深实时成交额</h2>
              </div>
              <span className={trendClass(turnover.delta)}>{signed(turnover.delta / 1e8, 0)}亿</span>
            </div>
            <div className="turnover-main">{formatMoney(turnover.current)}</div>
            <div className="turnover-caption">全天预测 {formatMoney(turnover.forecast)}</div>
            <div className="average-grid">
              <div><span>5日均</span><strong>{formatMoney(turnover.avg5)}</strong></div>
              <div><span>20日均</span><strong>{formatMoney(turnover.avg20)}</strong></div>
              <div><span>60日均</span><strong>{formatMoney(turnover.avg60)}</strong></div>
            </div>
          </section>

          <section className="panel breadth-panel">
            <div className="panel-heading compact">
              <div>
                <span className="panel-kicker">MARKET BREADTH</span>
                <h2>全市场涨跌分布</h2>
              </div>
              <div className="limit-counts">
                <span className="trend-up">涨停 {breadth.limitUp}</span>
                <span className="trend-down">跌停 {breadth.limitDown}</span>
              </div>
            </div>
            <div className="breadth-numbers">
              <strong className="trend-up">涨 {breadth.up}</strong>
              <span>平 {breadth.flat}</span>
              <strong className="trend-down">跌 {breadth.down}</strong>
            </div>
            <div className="breadth-track" aria-label={`下跌股票占比 ${Math.round(downRatio * 100)}%`}>
              <span className="up" style={{ width: `${Math.max(2, 100 - downRatio * 100)}%` }} />
              <span className="down" style={{ width: `${downRatio * 100}%` }} />
            </div>
            <Chart option={breadthOption} className="breadth-chart" ariaLabel="个股涨跌幅分布柱状图" />
          </section>
        </aside>
      </main>

      <section className="panel limit-panel">
        <div className="panel-heading">
          <div>
            <span className="panel-kicker">LIMIT-UP POOL</span>
            <h2>涨停梯队</h2>
          </div>
          <span className="panel-summary">当前展示 {limitPool.length} 只</span>
        </div>
        <div className="table-wrap">
          <table className="limit-table">
            <thead>
              <tr>
                <th>股票</th>
                <th>涨幅</th>
                <th>连板</th>
                <th>行业</th>
                <th>成交额</th>
                <th>首次封板</th>
              </tr>
            </thead>
            <tbody>
              {limitPool.map((stock) => (
                <tr key={`${stock.code}-${stock.name}`}>
                  <td data-label="股票"><strong>{stock.name}</strong><small>{stock.code}</small></td>
                  <td data-label="涨幅" className="trend-up">{signed(stock.changePct, 2)}%</td>
                  <td data-label="连板"><span className="board-badge">{stock.consecutive}板</span></td>
                  <td data-label="行业">{stock.industry}</td>
                  <td data-label="成交额">{formatMoney(stock.amount)}</td>
                  <td data-label="首次封板">{stock.firstLimitTime}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <footer>
        数据仅供研究与产品开发，不构成投资建议。公开行情接口可能延迟或中断，商业发布前应接入授权数据源。
      </footer>
    </div>
  );
}

export default App;
