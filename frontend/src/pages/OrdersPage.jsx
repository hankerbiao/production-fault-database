import React, { useCallback, useState } from "react";
import {
  Boxes,
  ClipboardList,
  Filter,
  PackageSearch,
  SlidersHorizontal,
} from "lucide-react";
import { listOrders, orderStats, orderDetail } from "../api/orders";
import { usePagedResource } from "../hooks/usePagedResource";
import { useDetail } from "../hooks/useDetail";
import { DetailPanel } from "../components/DetailPanel";
import { ExportButton } from "../components/ExportButton";
import { Hero } from "../components/Hero";
import { DateRangePicker } from "../components/DateRangePicker";
import { ModelSelect } from "../components/ModelSelect";
import { Pagination } from "../components/Pagination";
import { Stat, Stats } from "../components/Stats";
import { downloadCsv } from "../utils/export";
import {
  formatBusinessDate,
  formatDate,
  formatDateRange,
  formatNumber,
} from "../utils/formatters";
const initialFilters = {
  keyword: "",
  source: "",
  productionOrder: "",
  salesOrder: "",
  productModel: "",
  dateFrom: "",
  dateTo: "",
};
const initialStats = {
  total: 0,
  salesOrders: 0,
  dataStartDate: "",
  dataEndDate: "",
  latestSyncedAt: "",
  sg: 0,
  kk: 0,
  orderQuantity: 0,
  machineQuantity: 0,
  storageQuantity: 0,
};
export function OrdersPage({
  modelOptions = [],
  setConnected,
  setRefreshing,
  refreshToken = 0,
}) {
  const resource = usePagedResource({
    loadPage: useCallback((f, p, s) => listOrders(f, p, s), []),
    loadStats: useCallback((f) => orderStats(f), []),
    initialFilters,
    initialStats,
    refreshToken,
    onConnectionChange: setConnected,
    onRefreshing: setRefreshing,
  });
  const detail = useDetail(orderDetail, "无法读取该记录的详细字段");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const update = (key, value) =>
    resource.setFilters((prev) => ({ ...prev, [key]: value }));
  const submit = (event) => {
    event.preventDefault();
    resource.setPage(1);
    resource.load(1, resource.filters);
  };
  const reset = () => resource.reset(initialFilters);
  const exportData = async () => {
    setExporting(true);
    try {
      await downloadCsv({
        endpoint: "/api/orders",
        filters: resource.filters,
        filename: "销售订单明细.csv",
        columns: [
          { key: "source", label: "来源" },
          { key: "aufnr", label: "生产订单" },
          { key: "salesOrder", label: "销售订单" },
          { key: "customerId", label: "客户ID" },
          { key: "materialDescription", label: "物料描述" },
          { key: "productionModel", label: "生产机型" },
          {
            key: "plannedStartDate",
            label: "计划开始日期",
            value: (item) => formatBusinessDate(item.plannedStartDate),
          },
          { key: "orderQuantity", label: "订单数量" },
          { key: "storageQuantity", label: "入库数量" },
        ],
      });
    } finally {
      setExporting(false);
    }
  };
  const stats = resource.stats;
  return (
    <main>
      <Hero
        title="销售订单看板"
        detail="集中查看销售订单、生产订单和库存数量。"
        meta={[
          { label: "数据来源", value: "SAP 销售订单同步" },
          {
            label: "数据时间区间",
            value: formatDateRange(stats.dataStartDate, stats.dataEndDate),
          },
          { label: "最新数据时间", value: formatDate(stats.latestSyncedAt) },
        ]}
      />
      <Stats className="order-stats">
        <Stat
          icon={<Boxes />}
          label="订单记录总数"
          value={formatNumber(stats.total)}
          tone="blue"
        />
        <Stat
          icon={<ClipboardList />}
          label="销售订单数量（去重）"
          value={formatNumber(stats.salesOrders)}
          tone="orange"
        />
        <Stat
          icon={<PackageSearch />}
          label="SG 记录"
          value={formatNumber(stats.sg)}
          tone="green"
        />
        <Stat
          icon={<PackageSearch />}
          label="KK 记录"
          value={formatNumber(stats.kk)}
          tone="red"
        />
        <Stat
          icon={<ClipboardList />}
          label="订单数量"
          value={formatNumber(stats.orderQuantity)}
          tone="blue"
        />
        <Stat
          icon={<ClipboardList />}
          label="入库数量"
          value={formatNumber(stats.storageQuantity)}
          tone="orange"
        />
      </Stats>
      <section className="workspace">
        <div className="section-head">
          <div>
            <p className="eyebrow">SALES ORDERS</p>
            <h2>订单明细</h2>
          </div>
          <div className="section-head-meta">
            <span className="record-count">共 {resource.total} 条记录</span>
            <span className="record-summary">
              机器数量汇总：{formatNumber(stats.machineQuantity)}
            </span>
          </div>
        </div>
        <form className="filters order-filters" onSubmit={submit}>
          <select
            aria-label="订单来源"
            value={resource.filters.source}
            onChange={(e) => update("source", e.target.value)}
          >
            <option value="">全部来源</option>
            <option value="SG">SG</option>
            <option value="KK">KK</option>
          </select>
          <input
            className="filter-text"
            placeholder="生产订单"
            value={resource.filters.productionOrder}
            onChange={(e) => update("productionOrder", e.target.value)}
          />
          <input
            className="filter-text"
            placeholder="销售订单"
            value={resource.filters.salesOrder}
            onChange={(e) => update("salesOrder", e.target.value)}
          />
          <ModelSelect
            options={modelOptions}
            value={resource.filters.productModel}
            onChange={(value) => update("productModel", value)}
            placeholder="机型 / Product model"
          />
          <DateRangePicker
            from={resource.filters.dateFrom}
            to={resource.filters.dateTo}
            onChange={(from, to) =>
              resource.setFilters((prev) => ({
                ...prev,
                dateFrom: from,
                dateTo: to,
              }))
            }
            label="订单时间周期"
          />
          <button className="filter-btn" type="submit">
            <Filter size={16} />
            筛选
          </button>
          <button
            className="advanced-btn"
            type="button"
            onClick={() => setAdvancedOpen((value) => !value)}
            aria-expanded={advancedOpen}
          >
            <SlidersHorizontal size={16} />
            高级条件{advancedOpen ? "收起" : "展开"}
          </button>
          <button className="reset-btn" type="button" onClick={reset}>
            重置
          </button>
          <ExportButton exporting={exporting} onClick={exportData} />
          {advancedOpen && (
            <div className="advanced-filters">
              <input
                className="filter-text"
                placeholder="关键字"
                value={resource.filters.keyword}
                onChange={(e) => update("keyword", e.target.value)}
              />
            </div>
          )}
        </form>
        <div className="table-shell">
          <table className="orders-table">
            <thead>
              <tr>
                {[
                  "来源",
                  "生产订单",
                  "销售订单",
                  "客户",
                  "物料描述",
                  "生产机型",
                  "计划开始日期",
                  "订单数量",
                  "入库数量",
                ].map((label) => (
                  <th key={label}>{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {resource.items.map((item) => (
                <tr
                  className="record-row"
                  key={item.id}
                  onClick={() => detail.open(item.id)}
                >
                  <td>{item.source || "-"}</td>
                  <td>{item.aufnr || "-"}</td>
                  <td>{item.salesOrder || "-"}</td>
                  <td>{item.finalUser || item.customerId || "-"}</td>
                  <td>{item.materialDescription || "-"}</td>
                  <td>{item.productionModel || "-"}</td>
                  <td>{formatBusinessDate(item.plannedStartDate)}</td>
                  <td>{formatNumber(item.orderQuantity)}</td>
                  <td>{formatNumber(item.storageQuantity)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {resource.loading ? (
            <div className="table-loading" role="status">
              正在加载销售订单数据
            </div>
          ) : (
            !resource.items.length && (
              <div className="empty">
                <p>{resource.error || "没有找到匹配的数据"}</p>
              </div>
            )
          )}
        </div>
        <Pagination
          page={resource.page}
          total={resource.total}
          pageSize={20}
          setPage={resource.setPage}
        />
      </section>
      {(detail.loading || detail.detail) && (
        <DetailPanel
          detail={detail.detail}
          loading={detail.loading}
          close={detail.close}
          title="销售订单详情"
          heading={detail.detail?.order?.aufnr || "销售订单详情"}
        />
      )}
    </main>
  );
}
