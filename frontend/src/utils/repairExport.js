const repairFields = [
  ['MANDT', '集团'],
  ['PCODE', '主机条码'],
  ['ZWXDT', '维修日期时间'],
  ['ZMCOD1', '序列号'],
  ['ZRCOD1', '替换前原厂码'],
  ['MATNR', '物料号'],
  ['ZJXMC', '机型'],
  ['ZNGGZ', 'NG工站'],
  ['ZNGWD', '不良现象维度'],
  ['ZWXWD', '维修维度'],
  ['ZBJ', '涉及部件'],
  ['ZZRFL', '缺陷责任分类'],
  ['ZCCLH', '重插部件料号'],
  ['ZGZMS', '故障描述'],
  ['MAKTX', '物料描述（短文本）'],
  ['ZMCOD2', '替换后曙光码'],
  ['ZRCOD2', '替换后原厂码'],
  ['ZDATE', '读取日期'],
  ['ZTIME', '读取时间'],
  ['ZUSER', '读取人'],
  ['ZSOURCE', '数据来源'],
  ['ZDATE_WX', '维修日期'],
  ['REJUDGE', '复判人员'],
  ['RET', '复判结论'],
  ['RPDESC', '复判问题描述'],
  ['RNOTE', '复判备注'],
  ['SECFLG', '二次物料标识'],
  ['FACTORY', '生产基地'],
  ['ZWXWD1', '维修维度1'],
  ['ZWXWD2', '维修维度2'],
  ['ZWXWD3', '维修维度3'],
  ['U_FIX', '维修人员'],
  ['FIX_REMARKS', '维修人员备注维修信息'],
  ['TESTID', 'TESTID'],
  ['ZNGSPEC', 'NG工序'],
  ['T_FIND', 'NG时间'],
  ['ZNGWD1', '不良现象维度1'],
  ['ZNGWD2', '不良现象维度2'],
  ['ZNGWD3', '不良现象维度3'],
  ['ERROR_CODE', '错误码'],
  ['ERROR_MSG', '错误码描述'],
  ['RETEST_STATION', '重进产线站位名'],
  ['TEST_LOG_NAME', '测试日志名称'],
  ['SECOND_PART_NO', '重插物料序号'],
  ['RECORD01REPAIRM', '非关键件物料序号'],
  ['SLOT', '槽位'],
  ['AUFNR', '生产订单'],
  ['VBELN', '销售订单'],
  ['GSTRS', '计划开始时间'],
  ['POSNR', '行项目'],
  ['U_FIND', 'NG报工人员'],
  ['U_RMA_NAME', 'RMA复判人员'],
  ['RMA_RESULT', 'RMA复判结论'],
  ['RMA_TYPE2', '故障部件二级'],
  ['_source_key', '业务键'],
  ['_source_view', '源视图名'],
  ['_scope_run_id', '全量批次标识'],
  ['_sync_run_id', '同步批次标识'],
  ['_synced_at', '同步时间'],
  ['_id', '文档主键'],
];

const repairFieldLabels = Object.fromEntries(repairFields);

function cellValue(value) {
  if (value == null) return '';
  if (Array.isArray(value)) return JSON.stringify(value);
  if (typeof value === 'object') {
    if (typeof value.$date === 'string') return value.$date;
    if (typeof value.$oid === 'string') return value.$oid;
    if (typeof value.$numberLong === 'string') return value.$numberLong;
    return JSON.stringify(value);
  }
  return value;
}

function rawOf(item) {
  return item?.raw && typeof item.raw === 'object' ? item.raw : {};
}

export function repairExportColumns(rows) {
  const extras = new Set();
  for (const row of rows) {
    for (const key of Object.keys(rawOf(row))) {
      if (!Object.hasOwn(repairFieldLabels, key)) extras.add(key);
    }
  }
  return [
    ...repairFields.map(([key, label]) => ({ key, label, value: item => cellValue(rawOf(item)[key]) })),
    ...[...extras].sort().map(key => ({ key, label: key, value: item => cellValue(rawOf(item)[key]) })),
  ];
}
