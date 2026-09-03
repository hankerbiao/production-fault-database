import React, { useCallback, useState } from "react";
import {
  AlertTriangle,
  Boxes,
  ClipboardList,
  Eye,
  Filter,
  SlidersHorizontal,
} from "lucide-react";
import { listView, viewStats, viewDetail } from "../api/views";
import { usePagedResource } from "../hooks/usePagedResource";
import { useDetail } from "../hooks/useDetail";
import { DateRangePicker } from "../components/DateRangePicker";
import { DetailPanel } from "../components/DetailPanel";
import { ExportButton } from "../components/ExportButton";
import { Hero } from "../components/Hero";
import { ModelSelect } from "../components/ModelSelect";
import { Pagination } from "../components/Pagination";
import { Stat, Stats } from "../components/Stats";
import { downloadCsv } from "../utils/export";
import {
  formatBusinessDate,
  formatCell,
  formatDate,
  formatDateRange,
  formatNumber,
} from "../utils/formatters";
const emptyStats = {
  total: 0,
  salesOrders: 0,
  productionOrders: 0,
  missingSalesOrder: 0,
  missingProductionOrder: 0,
  missingProductionOrderDistinct: 0,
  dataStartDate: "",
  dataEndDate: "",
  latestSyncedAt: "",
};
export function ViewDashboardPage({
  config,
  modelOptions = [],
  setConnected,
  setRefreshing,
  refreshToken = 0,
}) {
  const initialFilters = {
    keyword: "",
    productionOrder: "",
    salesOrder: "",
    productModel: "",
    from: "",
    to: "",
    dateFrom: "",
    dateTo: "",
    stationCode: "",
    sn: "",
    base: "",
    materialCode: "",
    headOrder: "",
    itemOrder: "",
  };
  const resource = usePagedResource({
    loadPage: useCallback(
      (f, p, s) => listView(config.id, f, p, s),
      [config.id],
    ),
    loadStats: useCallback((f) => viewStats(config.id, f), [config.id]),
    initialFilters,
    initialStats: emptyStats,
    refreshToken,
    onConnectionChange: setConnected,
    onRefreshing: setRefreshing,
  });
  const detail = useDetail(
    (id) => viewDetail(config.id, id),
    "无法读取该记录的详细字段",
  );
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
  const exportData = async (
    extraFilters = resource.filters,
    filename = `${config.title}.csv`,
  ) => {
    setExporting(true);
    try {
      await downloadCsv({
        endpoint: `/api/views/${config.id}`,
        filters: extraFilters,
        filename,
        columns: config.columns.map((key) => ({
          key,
          label: config.columnLabels?.[key] || key,
        })),
      });
    } finally {
      setExporting(false);
    }
  };
  const bilingual = config.bilingual;
  const stats = resource.stats;
  return (
    <main>
      <Hero
        title={config.title}
        detail={config.detail}
        meta={[
          {
            label: bilingual ? "数据来源 / Data source" : "数据来源",
            value: config.source,
          },
          {
            label: bilingual ? "数据时间区间 / Data period" : "数据时间区间",
            value: formatDateRange(stats.dataStartDate, stats.dataEndDate),
          },
          {
            label: bilingual
              ? "最新数据时间 / Latest data time"
              : "最新数据时间",
            value: formatDate(stats.latestSyncedAt),
          },
        ]}
      />
      <Stats className="order-stats">
        <Stat
          icon={<Boxes />}
          label={bilingual ? "记录总数 / Total records" : "记录总数"}
          value={formatNumber(stats.total)}
          tone="blue"
        />
        {(config.stats || []).map((item) => (
          <Stat
            key={item.key}
            icon={
              item.tone === "red" || item.tone === "orange" ? (
                <AlertTriangle />
              ) : (
                <ClipboardList />
              )
            }
            label={item.label}
            value={formatNumber(stats[item.key])}
            tone={item.tone}
          />
        ))}
      </Stats>
      <section className="workspace">
        <div className="section-head">
          <div>
            <p className="eyebrow">{config.id}</p>
            <h2>
              {bilingual
                ? `${config.title} 明细 / Details`
                : `${config.title}明细`}
            </h2>
          </div>
          <span className="record-count">共 {resource.total} 条记录</span>
        </div>
        <form className="filters dashboard-filters" onSubmit={submit}>
          {config.showProductionOrder && (
            <input
              className="filter-text"
              placeholder="生产订单 / Production order"
              value={resource.filters.productionOrder}
              onChange={(e) => update("productionOrder", e.target.value)}
            />
          )}
          {config.showSalesOrder && (
            <input
              className="filter-text"
              placeholder="销售订单 / Sales order"
              value={resource.filters.salesOrder}
              onChange={(e) => update("salesOrder", e.target.value)}
            />
          )}
          {config.showProductModel && (
            <ModelSelect
              options={modelOptions}
              value={resource.filters.productModel}
              onChange={(value) => update("productModel", value)}
              placeholder={
                bilingual
                  ? "产品层次 / Product hierarchy"
                  : "机型 / Product model"
              }
            />
          )}
          {config.dateField && (
            <DateRangePicker
              from={resource.filters.from}
              to={resource.filters.to}
              onChange={(from, to) =>
                resource.setFilters((prev) => ({ ...prev, from, to }))
              }
              label="时间周期 / Date period"
              bilingual={bilingual}
            />
          )}
          <button className="filter-btn" type="submit">
            <Filter size={16} />
            筛选 / Filter
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
            重置 / Reset
          </button>
          <ExportButton
            exporting={exporting}
            onClick={() => exportData()}
            bilingual={bilingual}
          />
          {config.allowMissingSalesOrderExport && (
            <ExportButton
              exporting={exporting}
              onClick={() =>
                exportData(
                  { ...resource.filters, missingSalesOrder: true },
                  "订单BOM过账-销售订单为空.csv",
                )
              }
              label="导出销售订单为空"
              title="导出销售订单为空的 BOM 数据"
            />
          )}
          {advancedOpen && (
            <div className="advanced-filters">
              <input
                className="filter-text"
                placeholder="关键字 / Keyword"
                value={resource.filters.keyword}
                onChange={(e) => update("keyword", e.target.value)}
              />
              {config.advancedFields?.map((field) => (
                <input
                  key={field.key}
                  className="filter-text"
                  placeholder={field.placeholder}
                  value={resource.filters[field.key] || ""}
                  onChange={(e) => update(field.key, e.target.value)}
                />
              ))}
            </div>
          )}
        </form>
        <div className="table-shell">
          <table className="orders-table">
            <thead>
              <tr>
                {config.columns.map((column) => (
                  <th key={column}>
                    {config.columnLabels?.[column] || column}
                  </th>
                ))}
                <th>{bilingual ? "详情 / Details" : "详情"}</th>
              </tr>
            </thead>
            <tbody>
              {resource.items.map((item) => (
                <tr
                  className="record-row"
                  key={item.id}
                  onClick={() => detail.open(item.id)}
                >
                  {config.columns.map((column) => (
                    <td key={column}>{formatCell(item[column])}</td>
                  ))}
                  <td>
                    <button
                      className="icon-button"
                      title={
                        bilingual
                          ? "查看完整数据库字段 / View all database fields"
                          : "查看完整数据库字段"
                      }
                      onClick={(e) => {
                        e.stopPropagation();
                        detail.open(item.id);
                      }}
                    >
                      <Eye size={17} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {resource.loading ? (
            <div className="table-loading" role="status">
              正在加载{config.title}数据
            </div>
          ) : (
            !resource.items.length && (
              <div className="empty">
                <p>
                  {resource.error ||
                    (bilingual
                      ? "没有找到匹配的数据 / No matching records"
                      : "没有找到匹配的数据")}
                </p>
              </div>
            )
          )}
        </div>
        <Pagination
          page={resource.page}
          total={resource.total}
          pageSize={20}
          setPage={resource.setPage}
          bilingual={bilingual}
        />
      </section>
      {(detail.loading || detail.detail) && (
        <DetailPanel
          detail={detail.detail}
          loading={detail.loading}
          close={detail.close}
          title="视图记录详情 / View Record Details"
          heading="完整数据库字段 / All database fields"
        />
      )}
    </main>
  );
}
