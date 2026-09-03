package store

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/bson/primitive"
)

func normalizeFault(doc bson.M) Fault {
	return Fault{
		ID:                   firstText(doc, "_source_key", "_id"),
		HostBarcode:          firstText(doc, "PCODE"),
		SerialNumber:         firstText(doc, "ZMCOD1"),
		ErrorCode:            firstText(doc, "ERROR_CODE"),
		FaultDescription:     firstText(doc, "ZGZMS"),
		ErrorDescription:     firstText(doc, "ERROR_MSG"),
		ReviewProblem:        firstText(doc, "RPDESC"),
		DefectResponsibility: firstText(doc, "ZZRFL"),
		RepairAt:             repairTime(doc),
		ReadBy:               firstText(doc, "ZUSER"),
		RepairPerson:         firstText(doc, "U_FIX"),
		SalesOrder:           firstText(doc, "VBELN"),
		ProductionOrder:      firstText(doc, "AUFNR"),
		PlannedStartDate:     firstText(doc, "GSTRS"),
		MaterialCode:         firstText(doc, "MATNR"),
		MaterialDescription:  firstText(doc, "MAKTX"),
		NGStation:            firstText(doc, "ZNGGZ"),
		RetestStation:        firstText(doc, "RETEST_STATION"),
		RepairRemarks:        firstText(doc, "FIX_REMARKS"),
		Raw:                  cloneDocument(doc),
	}
}

func normalizeOrder(doc bson.M) Order {
	data, _ := doc["data"].(bson.M)
	return Order{
		ID:                  firstText(doc, "_id"),
		Source:              firstText(doc, "source"),
		AUFNR:               firstText(doc, "aufnr"),
		SalesOrder:          firstText(data, "VBELN"),
		CustomerID:          firstText(data, "KID"),
		FinalUser:           firstText(data, "NAME1_ZU"),
		MaterialDescription: firstText(data, "MAKTX"),
		ProductionModel:     firstText(data, "MAKTX_TH"),
		InventoryLocation:   firstText(data, "LGORT"),
		PlannedStartDate:    firstText(data, "GSTRS"),
		OrderQuantity:       documentQuantity(doc, "order_quantity", "GAMNG"),
		StorageQuantity:     documentQuantity(doc, "storage_quantity", "WMENG"),
		RecordCount:         int(number(doc["record_count"])),
		Raw:                 cloneDocument(doc),
	}
}

func cloneDocument(doc bson.M) bson.M {
	copy := make(bson.M, len(doc))
	for key, value := range doc {
		copy[key] = value
	}
	return copy
}

func detailFields(doc bson.M) []Field {
	priority := []string{"PCODE", "ZMCOD1", "ERROR_CODE", "ZGZMS", "ERROR_MSG", "ZZRFL", "MATNR", "MAKTX", "ZNGGZ", "RETEST_STATION", "VBELN", "AUFNR", "GSTRS", "POSNR", "ZWXDT", "ZDATE_WX", "ZTIME", "ZUSER", "U_FIX", "FIX_REMARKS", "RPDESC", "_synced_at"}
	seen := map[string]bool{}
	fields := make([]Field, 0, len(doc))
	appendField := func(key string) {
		if seen[key] {
			return
		}
		value, ok := doc[key]
		if !ok || value == nil {
			return
		}
		seen[key] = true
		label := repairFieldLabels[key]
		if label == "" {
			label = "未定义字段（" + key + "）"
		}
		fields = append(fields, Field{Key: key, Label: label, Value: fmt.Sprint(value)})
	}
	for _, key := range priority {
		appendField(key)
	}
	keys := make([]string, 0, len(doc))
	for key := range doc {
		if !seen[key] && key != "_id" && key != "_source_key" {
			keys = append(keys, key)
		}
	}
	sort.Strings(keys)
	for _, key := range keys {
		appendField(key)
	}
	return fields
}

func orderDetailFields(doc bson.M) []Field {
	fields := make([]Field, 0, len(orderDocumentLabels)+len(orderDataFieldLabels))
	for _, key := range []string{"_id", "source", "source_aufnr", "aufnr", "record_count", "order_quantity", "storage_quantity", "gstrs_date", "first_seen_at", "last_synced_at", "sync_count"} {
		if value, ok := doc[key]; ok && value != nil {
			fields = append(fields, Field{Key: key, Label: orderDocumentLabels[key], Value: fmt.Sprint(value)})
		}
	}
	data, _ := doc["data"].(bson.M)
	for _, key := range orderDataFieldOrder {
		if value, ok := data[key]; ok && value != nil {
			fields = append(fields, Field{Key: "data." + key, Label: orderDataFieldLabels[key], Value: fmt.Sprint(value)})
		}
	}
	return fields
}

var repairFieldLabels = map[string]string{
	"MANDT": "集团", "PCODE": "主机条码", "ZWXDT": "维修日期时间", "ZMCOD1": "序列号", "ZRCOD1": "替换前原厂码", "MATNR": "物料号", "ZJXMC": "机型", "ZNGGZ": "NG工站", "ZNGWD": "不良现象维度", "ZWXWD": "维修维度", "ZBJ": "涉及部件", "ZZRFL": "缺陷责任分类", "ZCCLH": "重插部件料号", "ZGZMS": "故障描述", "MAKTX": "物料描述（短文本）", "ZMCOD2": "替换后曙光码", "ZRCOD2": "替换后原厂码", "ZDATE": "读取日期", "ZTIME": "读取时间", "ZUSER": "读取人", "ZSOURCE": "数据来源", "ZDATE_WX": "维修日期", "REJUDGE": "复判人员", "RET": "复判结论", "RPDESC": "复判问题描述", "RNOTE": "复判备注", "SECFLG": "二次物料标识", "FACTORY": "生产基地", "ZWXWD1": "维修维度1", "ZWXWD2": "维修维度2", "ZWXWD3": "维修维度3", "U_FIX": "维修人员", "FIX_REMARKS": "维修人员备注维修信息", "TESTID": "TESTID", "ZNGSPEC": "NG工序", "T_FIND": "NG时间", "ZNGWD1": "不良现象维度1", "ZNGWD2": "不良现象维度2", "ZNGWD3": "不良现象维度3", "ERROR_CODE": "错误码", "ERROR_MSG": "错误码描述", "RETEST_STATION": "重进产线站位名", "TEST_LOG_NAME": "测试日志名称", "SECOND_PART_NO": "重插物料序号", "RECORD01REPAIRM": "非关键件物料序号", "SLOT": "槽位", "AUFNR": "生产订单", "VBELN": "销售订单", "GSTRS": "计划开始时间", "POSNR": "行项目", "U_FIND": "NG报工人员", "U_RMA_NAME": "RMA复判人员", "RMA_RESULT": "RMA复判结论", "RMA_TYPE2": "故障部件二级",
	"_source_view": "源视图名", "_scope_run_id": "全量批次标识", "_sync_run_id": "同步批次标识", "_synced_at": "同步时间",
}

var orderDocumentLabels = map[string]string{
	"_id": "订单文档主键", "source": "SAP来源", "source_aufnr": "来源与生产订单组合键", "aufnr": "生产订单", "record_count": "订单明细行数", "order_quantity": "订单数量", "storage_quantity": "入库数量", "gstrs_date": "计划开始日期", "first_seen_at": "首次写入时间", "last_synced_at": "最近同步时间", "sync_count": "成功同步次数",
}

var orderDataFieldLabels = map[string]string{
	"MANDT": "集团", "AUFNR": "生产订单", "VBELN": "销售订单", "POSNR": "行项目", "ETENR": "分批行", "MATNR": "物料编号", "MAKTX": "物料描述", "CPXH": "产品型号", "ZSTAT": "MES状态回传", "WERKS": "下单工厂", "IF_L6": "L6标识", "IF_l6": "L6标识", "KID": "客户ID", "GAMNG": "订单数量", "AUART": "订单类型", "LGORT": "库存地点", "NAME1_ZU": "最终用户", "MAKTX_TH": "生产机型", "WMENG": "入库数量", "GSTRS": "计划开始时间", "ZDATE_STARTED": "订单实际开始时间",
}

var orderDataFieldOrder = []string{"MANDT", "AUFNR", "VBELN", "POSNR", "ETENR", "MATNR", "MAKTX", "CPXH", "ZSTAT", "WERKS", "IF_L6", "IF_l6", "KID", "GAMNG", "AUART", "LGORT", "NAME1_ZU", "MAKTX_TH", "WMENG", "GSTRS", "ZDATE_STARTED"}

func firstText(doc bson.M, keys ...string) string {
	for _, key := range keys {
		if value, ok := doc[key]; ok && value != nil {
			result := strings.TrimSpace(fmt.Sprint(value))
			if result != "" && result != "<nil>" {
				return result
			}
		}
	}
	return ""
}

func timeText(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case time.Time:
		return typed.UTC().Format(time.RFC3339)
	case primitive.DateTime:
		return typed.Time().UTC().Format(time.RFC3339)
	default:
		return strings.TrimSpace(fmt.Sprint(value))
	}
}

func documentQuantity(doc bson.M, storedField, sourceField string) float64 {
	if value, ok := doc[storedField]; ok {
		return number(value)
	}
	if records, ok := doc["records"].(bson.A); ok {
		total := 0.0
		for _, record := range records {
			if row, ok := record.(bson.M); ok {
				total += number(row[sourceField])
			}
		}
		return total
	}
	if data, ok := doc["data"].(bson.M); ok {
		return number(data[sourceField])
	}
	return 0
}

func repairTime(doc bson.M) time.Time {
	if result := parseTime(doc, "ZWXDT"); !result.IsZero() {
		return result
	}
	if repairDate, repairClock := firstText(doc, "ZDATE_WX"), firstText(doc, "ZTIME"); repairDate != "" && repairClock != "" {
		if result := parseTimeText(repairDate + " " + repairClock); !result.IsZero() {
			return result
		}
	}
	if readDate, readClock := firstText(doc, "ZDATE"), firstText(doc, "ZTIME"); readDate != "" && readClock != "" {
		if result := parseTimeText(readDate + " " + readClock); !result.IsZero() {
			return result
		}
	}
	return parseTime(doc, "ZDATE_WX", "ZDATE", "_synced_at")
}

func parseTime(doc bson.M, keys ...string) time.Time {
	for _, key := range keys {
		value, ok := doc[key]
		if !ok {
			continue
		}
		if result, ok := value.(time.Time); ok {
			return result
		}
		if result, ok := value.(primitive.DateTime); ok {
			return result.Time()
		}
		raw, ok := value.(string)
		if !ok {
			continue
		}
		if result := parseTimeText(raw); !result.IsZero() {
			return result
		}
	}
	return time.Time{}
}

func parseTimeText(raw string) time.Time {
	for _, layout := range []string{"20060102 150405", "20060102150405", "2006-01-02 15:04:05", "2006/01/02 15:04:05", "20060102", time.RFC3339, "2006-01-02"} {
		if result, err := time.Parse(layout, strings.TrimSpace(raw)); err == nil {
			return result
		}
	}
	return time.Time{}
}
func number(value any) float64 {
	switch v := value.(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int:
		return float64(v)
	case int32:
		return float64(v)
	case int64:
		return float64(v)
	default:
		var result float64
		_, _ = fmt.Sscan(fmt.Sprint(value), &result)
		return result
	}
}
