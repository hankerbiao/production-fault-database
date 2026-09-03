package store

import (
	"fmt"
	"sort"

	"go.mongodb.org/mongo-driver/bson"
)

var stationFieldOrder = []string{
	"MANDT", "HISTROYID", "PCODE", "OCODE", "AUFNR", "KDAUF", "KDPOS", "LGORT", "MAKTX_TH",
	"LEAD_CYCLE", "AUFNR_CYCLE", "CUSTOMIZE_CYCLE", "OEMBZ", "SPEC", "OPERATION", "SPEC_DESC",
	"UNAME", "LASTSPEC_TIME", "SPEC_TIME", "GSTRS", "PLAN_END_TIME", "ACTUAL_START_TIME", "ACTUAL_END_TIME",
	"HADE1", "HADE2", "HADE3", "HADE4", "HADE5", "HADE6", "HADE7", "HADE8", "STATU", "MESS",
	"CLASSCODE", "EQUIPMENTNUMBER", "ERR_FLAG", "LINE_CODE", "NEXT_SECTION", "PASSCOUNT", "TEST_ID", "PRODH",
}

var stationFieldLabels = map[string]string{
	"MANDT": "集团", "HISTROYID": "主键", "PCODE": "主机序列号", "OCODE": "客户序列号", "AUFNR": "生产订单",
	"KDAUF": "销售订单", "KDPOS": "行项目", "LGORT": "基地", "MAKTX_TH": "生产机型", "LEAD_CYCLE": "生产周期（小时）",
	"AUFNR_CYCLE": "订单类型周期（小时）", "CUSTOMIZE_CYCLE": "定制化周期（小时）", "OEMBZ": "是否定制化", "SPEC": "操作工序",
	"OPERATION": "操作说明", "SPEC_DESC": "工序描述", "UNAME": "操作员", "LASTSPEC_TIME": "上一工序时间", "SPEC_TIME": "操作时间",
	"GSTRS": "计划开始日期", "PLAN_END_TIME": "计划结束时间", "ACTUAL_START_TIME": "实际开始时间", "ACTUAL_END_TIME": "实际结束时间",
	"HADE1": "预留字段1", "HADE2": "预留字段2", "HADE3": "预留字段3", "HADE4": "预留字段4", "HADE5": "预留字段5",
	"HADE6": "预留字段6", "HADE7": "预留字段7", "HADE8": "预留字段8", "STATU": "返回状态", "MESS": "返回值",
	"CLASSCODE": "班次编码", "EQUIPMENTNUMBER": "设备编号", "ERR_FLAG": "检验结果", "LINE_CODE": "生产线", "NEXT_SECTION": "下一工序",
	"PASSCOUNT": "通过次数", "TEST_ID": "检验ID", "PRODH": "产品层次",
}

var serialBindingFieldOrder = []string{"ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH"}
var serialBindingFieldLabels = map[string]string{
	"ZCODE_HEAD": "大刀/机头序列号（ZCODE_HEAD）",
	"ZCODE_ITEM": "小刀/BOX序列号（ZCODE_ITEM）",
	"AUFNR_HEAD": "大刀/机头生产订单号（AUFNR_HEAD）",
	"AUFNR_ITEM": "小刀/BOX生产订单号（AUFNR_ITEM）",
	"PRODH":      "产品层次（PRODH）",
}

var bomPostingFieldOrder = []string{
	"MANDT", "WERKS", "MATNR", "BWART", "BUDAT_MKPF", "LGORT", "MENGE_A", "KUNNR_A", "MAKTX", "NAME1_A",
	"TYPE", "VGBEL_A", "VGPOS_A", "MATNR_SC", "MATKX_SC2", "AUFNR_1", "POSNR_EX", "VBELN_EX", "KUNNR_EX", "NAME1_EX",
	"AUART", "KUNNR_1", "NAME1_X", "MAKTX_CP", "MATNR_CP", "PSMNG", "MATKL", "CXFLG", "WGBEZ", "CPX",
	"BU", "MATNR_BI", "ZSTAT", "MBLNR", "MJAHR", "ZEILE", "FDATU_O", "DATE_JH_O", "ETENR_O", "MAT_KDAUF",
	"MAT_KDPOS", "VGBEL", "VGPOS", "KUNNR", "NAME1", "MENGE", "USNAM_MKPF", "VSNMR_V", "ZNAM",
}

var bomPostingFieldLabels = map[string]string{
	"MANDT": "集团", "WERKS": "工厂", "MATNR": "物料号", "BWART": "移动类型", "BUDAT_MKPF": "过账日期",
	"LGORT": "库存地点", "MENGE_A": "过账数量", "KUNNR_A": "关联客户编号", "MAKTX": "物料描述", "NAME1_A": "关联客户名称",
	"TYPE": "业务类型", "VGBEL_A": "关联前序单据", "VGPOS_A": "关联前序单据行项目", "MATNR_SC": "子件物料号", "MATKX_SC2": "子件物料描述",
	"AUFNR_1": "生产订单", "POSNR_EX": "销售订单行项目", "VBELN_EX": "销售订单", "KUNNR_EX": "销售订单客户编号", "NAME1_EX": "销售订单客户名称",
	"AUART": "订单类型", "KUNNR_1": "客户编号", "NAME1_X": "客户名称", "MAKTX_CP": "成品物料描述", "MATNR_CP": "成品物料号",
	"PSMNG": "需求数量", "MATKL": "物料组", "CXFLG": "冲销标识", "WGBEZ": "物料组描述", "CPX": "公司",
	"BU": "事业部", "MATNR_BI": "BOM物料号", "ZSTAT": "状态", "MBLNR": "物料凭证号", "MJAHR": "物料凭证年度",
	"ZEILE": "物料凭证行项目", "FDATU_O": "交货日期", "DATE_JH_O": "计划交货日期", "ETENR_O": "计划行", "MAT_KDAUF": "物料销售订单",
	"MAT_KDPOS": "物料销售订单行项目", "VGBEL": "前序单据", "VGPOS": "前序单据行项目", "KUNNR": "客户编号", "NAME1": "客户名称",
	"MENGE": "数量", "USNAM_MKPF": "过账用户", "VSNMR_V": "版本号", "ZNAM": "名称",
}

var viewMetadataFieldLabels = map[string]string{
	"_source_key":   "源记录键",
	"_source_view":  "源视图",
	"_scope_run_id": "清理批次标识",
	"_sync_run_id":  "同步批次标识",
	"_synced_at":    "同步时间",
}

func viewDetailFields(viewID string, doc bson.M) []Field {
	keys := make([]string, 0, len(doc))
	seen := make(map[string]bool, len(doc))
	if viewID == "Z_V_ZMES_T_001" {
		keys = append(keys, stationFieldOrder...)
		for _, key := range keys {
			seen[key] = true
		}
	} else if viewID == "ZSGV_ZPP_SERNOLIST" {
		keys = append(keys, serialBindingFieldOrder...)
		for _, key := range keys {
			seen[key] = true
		}
	} else if viewID == "ZSGV_ZSD124" {
		keys = append(keys, bomPostingFieldOrder...)
		for _, key := range keys {
			seen[key] = true
		}
	}
	remaining := make([]string, 0, len(doc))
	for key := range doc {
		if key != "id" && key != "_id" && !seen[key] {
			remaining = append(remaining, key)
		}
	}
	sort.Strings(remaining)
	keys = append(keys, remaining...)
	result := make([]Field, 0, len(keys))
	for _, key := range keys {
		value := doc[key]
		if value != nil {
			label := "字段 / Field（" + key + "）"
			if translated, ok := viewMetadataFieldLabels[key]; ok {
				label = translated
			}
			if viewID == "Z_V_ZMES_T_001" {
				if translated, ok := stationFieldLabels[key]; ok {
					label = translated
				}
			} else if viewID == "ZSGV_ZPP_SERNOLIST" {
				if translated, ok := serialBindingFieldLabels[key]; ok {
					label = translated
				}
			} else if viewID == "ZSGV_ZSD124" {
				if translated, ok := bomPostingFieldLabels[key]; ok {
					label = translated
				}
			}
			result = append(result, Field{Key: key, Label: label, Value: fmt.Sprint(value)})
		}
	}
	return result
}
