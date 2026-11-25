import { useMemo, useState } from 'react';
import { Boxes, Check, Copy, Download, Hash, Layers, Search, Server, TriangleAlert } from 'lucide-react';
import { machineConfig } from '../api/machines';
import { formatNumber } from '../utils/formatters';

const typeLabels = {
  CPU: '处理器', MEM: '内存', YP: '硬盘 / 存储', MB: '主板', DYMK: '电源模块', FS: '风扇模组',
  JX: '机箱', NETC: '网卡', XK: '显卡', QTBK: '其他部件', XL: '线缆',
};
const typeOrder = ['CPU', 'MEM', 'YP', 'MB', 'DYMK', 'FS', 'JX', 'NETC', 'XK', 'QTBK', 'XL', ''];
const examples = ['6102310604796170', '6102299104767755', '6102260704679259'];
const fieldDocs = [
  ['needNum', '需求数量', 'stepsmanagement 上游字段，本地数据无对应来源，固定为 null。', 'null'],
  ['num', '单机数量', '订单净发料量除以订单台数 PSMNG；发料未齐时可能是小数。', '2'],
  ['totalNum', '订单合计', '该订单内有符号净发料总量：261 发料为正、262 退料为负。', '146'],
  ['name', '物料描述', 'BOM 过账明细中的物料描述 MAKTX，用于展示部件名称。', 'Intel 8457C …'],
  ['materialgroup', '物料组', '物料组 MATKL，同时用于映射部件类型和排除包装、标签、螺钉。', '620002'],
  ['dn', '物料号', '物料主数据编号 MATNR，同一物料在订单内汇总为一项。', 'B6200004'],
  ['relationId', '关联键', '本地生成的代理关联键，格式为 local-<物料号>，不是 steps 原主键。', 'local-B6200004'],
  ['id', '记录主键', '本地生成的代理主键，格式为 local-<生产订单>-<物料号>。', 'local-30240235-B6200004'],
  ['type', '部件类型', '由物料组映射的 steps 类型代码；未映射的物料组返回空字符串。', 'CPU'],
  ['params', '参数数组', '该物料的型号、库位等键值对，字段说明见下表参数部分。', '[ … ]'],
];
const paramFieldDocs = [
  ['comparionValue', '参数值', '具体取值，如型号 AP-CA3200F54B1、库位 1N00；沿用 steps 原字段拼写。', 'AP-CA3200F54B1'],
  ['name', '参数名', '参数语义，目前输出“型号”和“库位”。', '型号'],
  ['partsDescription', '部件类别', '物料所属部件的中文类别，如处理器、电源模块、线缆。', '电源模块'],
  ['materialGroup', '物料组', '参数所属物料的 MATKL，与顶层 materialgroup 一致。', '330002'],
  ['orderId', '生产订单号', '该物料归属的生产订单 AUFNR，数值类型。', '30240235'],
  ['materialSn', '物料号', '参数所属物料的 MATNR，与顶层 dn 一致。', '33000348'],
  ['relationId', '关联键', '参数指向的本地代理关联键，与顶层 relationId 一致。', 'local-33000348'],
  ['id', '参数主键', '本地生成的参数代理主键，格式为 local-<订单>-<物料号>-<参数名>。', 'local-30240235-33000348-model'],
];

function FieldDocs() {
  return <details className="mc-field-docs" open>
    <summary>字段说明 <span>顶层 {fieldDocs.length} 个字段 · 参数 {paramFieldDocs.length} 个字段</span></summary>
    <p className="mc-field-note">返回结构与 stepsmanagement queryMaterialByOrderID 兼容。id、relationId 为本地代理键，needNum 无数据来源。</p>
    <div className="mc-field-table-wrap">
      <table className="mc-field-table">
        <thead><tr><th>字段</th><th>说明</th><th>示例值</th></tr></thead>
        <tbody>{fieldDocs.map(([field, label, description, example]) => <tr key={field}>
          <th scope="row"><code>{field}</code><small>{label}</small></th>
          <td>{description}</td>
          <td><code>{example}</code></td>
        </tr>)}</tbody>
      </table>
      <table className="mc-field-table mc-field-table-params">
        <thead><tr><th>params 内字段</th><th>说明</th><th>示例值</th></tr></thead>
        <tbody>{paramFieldDocs.map(([field, label, description, example]) => <tr key={field}>
          <th scope="row"><code>{field}</code><small>{label}</small></th>
          <td>{description}</td>
          <td><code>{example}</code></td>
        </tr>)}</tbody>
      </table>
    </div>
  </details>;
}

function typeLabel(type) {
  return typeLabels[type] || (type ? type : '未分类');
}

function paramValue(item, names) {
  return item.params?.find(parameter => names.includes(parameter.name))?.comparionValue || '';
}

function productionOrder(items) {
  return items.find(item => item.params?.length)?.params[0]?.orderId || '';
}

function groupItems(items) {
  const groups = new Map();
  items.forEach(item => {
    const key = item.type || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return [...groups.entries()].sort(([left], [right]) => {
    const leftIndex = typeOrder.indexOf(left);
    const rightIndex = typeOrder.indexOf(right);
    return (leftIndex < 0 ? typeOrder.length : leftIndex) - (rightIndex < 0 ? typeOrder.length : rightIndex);
  });
}

function MaterialCard({ item }) {
  const model = paramValue(item, ['型号']);
  const location = paramValue(item, ['库位']);
  return <article className="mc-material">
    <div className="mc-material-head">
      <span className={`mc-type mc-type-${(item.type || 'other').toLowerCase()}`}>{typeLabel(item.type)}</span>
      <span className="mc-material-code">{item.dn}</span>
    </div>
    <h3>{item.name || '未命名物料'}</h3>
    <div className="mc-material-meta">
      <span><b>单机</b> × {formatNumber(item.num)}</span>
      <span><b>订单合计</b> {formatNumber(item.totalNum)}</span>
      <span><b>物料组</b> {item.materialgroup || '-'}</span>
    </div>
    {(model || location) && <div className="mc-params">
      {model && <span><b>型号</b>{model}</span>}
      {location && <span><b>库位</b>{location}</span>}
    </div>}
  </article>;
}

export function MachineConfigPage({ setConnected } = {}) {
  const [sn, setSn] = useState('');
  const [source, setSource] = useState('5000');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);

  const order = result ? productionOrder(result) : '';
  const groups = useMemo(() => result ? groupItems(result) : [], [result]);
  const categoryCount = groups.filter(([type]) => type).length;
  const unitTotal = result ? result.reduce((sum, item) => sum + (Number(item.num) || 0), 0) : 0;

  async function submit(event) {
    event.preventDefault();
    const value = sn.trim();
    if (!value) { setError('请输入整机 SN。'); return; }
    setError('');
    setLoading(true);
    try {
      const items = await machineConfig(value, source);
      setResult(Array.isArray(items) ? items : []);
      setSn(value);
      setConnected?.(true);
    } catch (requestError) {
      setResult(null);
      setError(requestError.message || '查询失败');
      setConnected?.(false);
    } finally {
      setLoading(false);
    }
  }

  function useExample(value) {
    setSn(value);
    setError('');
  }

  async function copyJSON() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(result, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* Clipboard access can be denied by the browser. */ }
  }

  function downloadJSON() {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `machine-config-${sn}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  return <main className="machine-config-page">
    <section className="hero mc-hero">
      <div>
        <p className="eyebrow">MACHINE / CONFIGURATION LOOKUP</p>
        <h1>机器配置查询</h1>
        <p className="intro">输入整机 SN，通过工位记录和订单 BOM 过账明细查询机器的物料配置。</p>
      </div>
      <div className="hero-meta">
        <div><span>数据链路</span><strong>SN → AUFNR → BOM</strong></div>
        <div><span>数据源</span><strong>{source === '5000' ? '5000 · 本地数据库' : 'HANA · 全量数据'}</strong></div>
      </div>
    </section>

    <section className="workspace mc-workspace">
      <form className="mc-search" onSubmit={submit}>
        <fieldset className="mc-source" disabled={loading}>
          <legend>数据源</legend>
          <label><input type="radio" name="machine-source" value="5000" checked={source === '5000'} onChange={() => { setSource('5000'); setResult(null); setError(''); }} />5000 · 本地数据库</label>
          <label><input type="radio" name="machine-source" value="hana" checked={source === 'hana'} onChange={() => { setSource('hana'); setResult(null); setError(''); }} />HANA · 全量数据</label>
        </fieldset>
        <div className="mc-search-field">
          <label htmlFor="machine-sn">整机序列号 · SN</label>
          <div className="mc-search-input">
            <Hash size={17} />
            <input id="machine-sn" value={sn} onChange={event => setSn(event.target.value)} placeholder="例如 6102310604796170" maxLength={40} autoComplete="off" />
          </div>
        </div>
        <button className="mc-search-button" type="submit" disabled={loading}>
          <Search size={16} />{loading ? '查询中…' : '查询配置'}
        </button>
      </form>

      <div className="mc-examples">
        <span>试试示例</span>
        {examples.map(value => <button type="button" key={value} onClick={() => useExample(value)}>{value}</button>)}
      </div>

      {error && <div className="mc-error" role="alert"><TriangleAlert size={16} /><div><strong>查询失败</strong><p>{error}</p></div></div>}

      {!result && !error && !loading && <section className="mc-placeholder">
        <Server size={30} />
        <strong>等待查询</strong>
        <p>输入整机 SN 后，这里会按部件类别展示 CPU、内存、电源、风扇、线缆等配置明细。</p>
      </section>}

      {result && result.length === 0 && <section className="mc-placeholder">
        <Boxes size={30} />
        <strong>该订单暂无物料明细</strong>
        <p>已匹配到生产订单，但 BOM 过账明细为空。可稍后重试，或检查该订单是否已过账。</p>
      </section>}

      {result && result.length > 0 && <>
        <div className="mc-summary">
          <div><span>生产订单 · AUFNR</span><strong>{order || '-'}</strong></div>
          <div><span>物料项数</span><strong>{result.length}</strong></div>
          <div><span>部件类别</span><strong>{categoryCount}</strong></div>
          <div><span>单机物料总数</span><strong>{formatNumber(unitTotal)}</strong></div>
        </div>

        <div className="section-head mc-result-head">
          <div><p className="eyebrow">CONFIGURATION BREAKDOWN</p><h2>配置明细</h2></div>
          <div className="mc-result-actions">
            <span className="record-count">SN {sn}</span>
            <button type="button" onClick={copyJSON}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? '已复制' : '复制 JSON'}</button>
            <button type="button" onClick={downloadJSON}><Download size={14} />下载 JSON</button>
          </div>
        </div>

        <div className="mc-groups">
          {groups.map(([type, items]) => <section className="mc-group" key={type || 'other'}>
            <div className="mc-group-head">
              <h3><Layers size={15} />{typeLabel(type)}</h3>
              <span>{items.length} 项</span>
            </div>
            <div className="mc-grid">{items.map(item => <MaterialCard key={item.id || item.dn} item={item} />)}</div>
          </section>)}
        </div>

        <details className="mc-raw">
          <summary>查看原始返回 JSON</summary>
          <pre>{JSON.stringify(result, null, 2)}</pre>
        </details>
      </>}

      <FieldDocs />
    </section>
  </main>;
}
