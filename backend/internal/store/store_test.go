package store

import (
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/bson/primitive"
	"go.mongodb.org/mongo-driver/mongo"
)

func TestRepairFilterEscapesRegexAndCombinesConditions(t *testing.T) {
	filter := repairFilter(Filters{Keyword: "a+b", HostBarcode: "PC-1", SalesOrder: "SO-1"})
	conditions, ok := filter["$and"].(primitive.A)
	if !ok || len(conditions) != 3 {
		t.Fatalf("filter=%v", filter)
	}
	keyword := conditions[2].(bson.M)["$or"].(primitive.A)[0].(bson.M)["ERROR_CODE"].(bson.M)
	if keyword["$regex"] != "a\\+b" || keyword["$options"] != "i" {
		t.Fatalf("keyword=%v", keyword)
	}
}

func TestRepairPlannedDateRangeFilterSupportsISOAndSAPDates(t *testing.T) {
	filter := repairFilter(Filters{TimeField: "planned", DateFrom: "2026-08-04", DateTo: "20260902"})
	conditions := filter["$and"].(primitive.A)
	branches := conditions[0].(bson.M)["$or"].(primitive.A)
	if len(branches) != 2 {
		t.Fatalf("date branches=%v", branches)
	}
	iso := branches[0].(bson.M)["GSTRS"].(bson.M)
	if iso["$gte"] != "2026-08-04" || iso["$lte"] != "2026-09-02" {
		t.Fatalf("ISO date range=%v", iso)
	}
	compact := branches[1].(bson.M)["GSTRS"].(bson.M)
	if compact["$gte"] != "20260804" || compact["$lte"] != "20260902" {
		t.Fatalf("compact date range=%v", compact)
	}
}

func TestOrderFilterAndMissingField(t *testing.T) {
	filter := orderFilter(OrderFilters{Source: "SG", GSTRSFrom: "2026-01-01", GSTRSTo: "2026-01-31", Keyword: "SO"})
	if len(filter["$and"].(primitive.A)) != 3 {
		t.Fatalf("filter=%v", filter)
	}
	missing := missingField("VBELN")
	if len(missing["$or"].(primitive.A)) != 3 {
		t.Fatalf("missing=%v", missing)
	}
}

func TestOrderDateRangeFilterSupportsISOAndSAPDates(t *testing.T) {
	filter := orderFilter(OrderFilters{DateFrom: "2026-08-04", DateTo: "20260902"})
	conditions := filter["$and"].(primitive.A)
	if len(conditions) != 1 {
		t.Fatalf("filter=%v", filter)
	}
	branches := conditions[0].(bson.M)["$or"].(primitive.A)
	if len(branches) != 3 {
		t.Fatalf("date branches=%v", branches)
	}
	normalized := branches[0].(bson.M)["gstrs_date"].(bson.M)
	if normalized["$gte"] != "2026-08-04" || normalized["$lte"] != "2026-09-02" {
		t.Fatalf("normalized date range=%v", normalized)
	}
	compact := branches[2].(bson.M)["data.GSTRS"].(bson.M)
	if compact["$gte"] != "20260804" || compact["$lte"] != "20260902" {
		t.Fatalf("compact date range=%v", compact)
	}
}

func TestBatchAndLeadingZeroFilters(t *testing.T) {
	filter := repairFilter(Filters{SNS: "SN001，SN002", ProductionOrders: "123,0000456"})
	conditions := filter["$and"].(primitive.A)
	if len(conditions) != 2 {
		t.Fatalf("filter=%v", filter)
	}
	production := conditions[1].(bson.M)["$or"].(primitive.A)
	if len(production) != 2 {
		t.Fatalf("production batch=%v", production)
	}
	first := production[0].(bson.M)["AUFNR"].(bson.M)
	if first["$regex"] != "^0*123$" {
		t.Fatalf("leading-zero regex=%v", first)
	}
}

func TestViewFilterIncludesEndDateForStationDatetimes(t *testing.T) {
	filter := viewFilter("Z_V_ZMES_T_001", ViewFilters{DateTo: "2026-03-31"}, nil, "ACTUAL_START_TIME")
	conditions := filter["$and"].(primitive.A)
	dateCondition := conditions[0].(bson.M)["ACTUAL_START_TIME"].(bson.M)
	if dateCondition["$lte"] != "2026-03-31 23:59:59" {
		t.Fatalf("end date=%v", dateCondition["$lte"])
	}
}

func TestBOMViewFilterIncludesEmptySalesOrders(t *testing.T) {
	filter := viewFilter("ZSGV_ZSD124", ViewFilters{MissingSalesOrder: true}, nil, "BUDAT_MKPF")
	conditions := filter["$and"].(primitive.A)
	if len(conditions) != 1 {
		t.Fatalf("filter=%v", filter)
	}
	expr, ok := conditions[0].(bson.M)["$expr"].(bson.M)
	if !ok || expr["$eq"] == nil {
		t.Fatalf("empty sales order condition=%v", conditions[0])
	}
}

func TestStatsUseSingleAggregationPipeline(t *testing.T) {
	for name, pipeline := range map[string]mongo.Pipeline{
		"repairs": repairStatsPipeline(repairFilter(Filters{HostBarcode: "PC-1"}), "repair"),
		"orders":  orderStatsPipeline(orderFilter(OrderFilters{Source: "SG"})),
	} {
		if len(pipeline) != 3 {
			t.Fatalf("%s pipeline has %d stages, want match/group/project", name, len(pipeline))
		}
		if _, ok := pipeline[0].Map()["$match"]; !ok {
			t.Fatalf("%s first stage=%v", name, pipeline[0])
		}
		if _, ok := pipeline[1].Map()["$group"]; !ok {
			t.Fatalf("%s second stage=%v", name, pipeline[1])
		}
		if _, ok := pipeline[2].Map()["$project"]; !ok {
			t.Fatalf("%s third stage=%v", name, pipeline[2])
		}
	}
}

func TestStationViewStatsPipelineCountsMissingOrders(t *testing.T) {
	pipeline := viewStatsPipeline("Z_V_ZMES_T_001", bson.M{"PCODE": "PC-1"}, "ACTUAL_START_TIME")
	group := pipeline[1].Map()["$group"].(bson.M)
	project := pipeline[2].Map()["$project"].(bson.M)
	if _, ok := group["missingSalesOrder"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if _, ok := group["missingProductionOrder"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if project["missingSalesOrder"] != 1 || project["missingProductionOrder"] != 1 {
		t.Fatalf("project=%v", project)
	}
}

func TestBOMViewStatsPipelineCountsDistinctOrders(t *testing.T) {
	pipeline := viewStatsPipeline("ZSGV_ZSD124", bson.M{"MATNR": "MAT-1"}, "BUDAT_MKPF")
	group := pipeline[1].Map()["$group"].(bson.M)
	project := pipeline[2].Map()["$project"].(bson.M)
	if _, ok := group["productionOrderValues"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if _, ok := group["salesOrderValues"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if _, ok := project["productionOrders"]; !ok {
		t.Fatalf("project=%v", project)
	}
	if _, ok := project["salesOrders"]; !ok {
		t.Fatalf("project=%v", project)
	}
	if _, ok := group["missingProductionOrder"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if _, ok := group["missingProductionOrderValues"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if _, ok := group["missingSalesOrder"]; !ok {
		t.Fatalf("group=%v", group)
	}
	if _, ok := project["missingProductionOrderDistinct"]; !ok {
		t.Fatalf("project=%v", project)
	}
	if project["missingProductionOrder"] != 1 || project["missingSalesOrder"] != 1 {
		t.Fatalf("project=%v", project)
	}
}

func TestOrderStatsPipelineExposesMachineQuantity(t *testing.T) {
	pipeline := orderStatsPipeline(orderFilter(OrderFilters{Source: "SG"}))
	project := pipeline[2].Map()["$project"].(bson.M)
	if project["machineQuantity"] != "$orderQuantity" {
		t.Fatalf("machineQuantity projection=%v", project["machineQuantity"])
	}
}

func TestNormalizeFaultAndOrder(t *testing.T) {
	doc := bson.M{"_source_key": "key", "PCODE": " PC-1 ", "ZMCOD1": "SN-1", "ERROR_CODE": "E1", "ZDATE_WX": "20260102", "ZTIME": "030405", "VBELN": "SO1", "AUFNR": "PO1", "GSTRS": "20260101", "_synced_at": primitive.NewDateTimeFromTime(time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC))}
	fault := normalizeFault(doc)
	if fault.ID != "key" || fault.HostBarcode != "PC-1" || fault.RepairAt.IsZero() || fault.SalesOrder != "SO1" || fault.PlannedStartDate != "20260101" {
		t.Fatalf("fault=%+v", fault)
	}

	order := normalizeOrder(bson.M{"_id": "SG:PO1", "source": "SG", "aufnr": "PO1", "data": bson.M{"VBELN": "SO1", "GAMNG": "3", "WMENG": "2", "GSTRS": "20260102"}, "records": bson.A{bson.M{"GAMNG": "1", "WMENG": "4"}}, "record_count": 1})
	if order.ID != "SG:PO1" || order.OrderQuantity != 1 || order.StorageQuantity != 4 || order.PlannedStartDate != "20260102" {
		t.Fatalf("order=%+v", order)
	}
}

func TestDetailFieldsOrderingAndFallbacks(t *testing.T) {
	fields := detailFields(bson.M{"ZZRFL": "责任", "PCODE": "PC", "CUSTOM": "v", "_id": "ignored", "_source_key": "ignored"})
	if len(fields) != 3 || fields[0].Key != "PCODE" || fields[1].Key != "ZZRFL" || fields[2].Key != "CUSTOM" {
		t.Fatalf("fields=%+v", fields)
	}
	if firstText(bson.M{"a": " ", "b": 12}, "a", "b") != "12" {
		t.Fatal("firstText fallback failed")
	}
	if !parseTimeText("2026-01-02 03:04:05").Equal(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)) {
		t.Fatal("parseTimeText failed")
	}
}

func TestStationDetailFieldsUseDocumentedChineseLabelsAndOrder(t *testing.T) {
	doc := bson.M{
		"PRODH": "00100", "CLASSCODE": "A", "PCODE": "PC-1", "ACTUAL_END_TIME": "2026-01-02 03:04:05",
		"_id": "ignored",
	}
	fields := viewDetailFields("Z_V_ZMES_T_001", doc)
	if len(fields) != 4 {
		t.Fatalf("fields=%+v", fields)
	}
	if fields[0].Key != "PCODE" || fields[0].Label != "主机序列号" {
		t.Fatalf("first field=%+v", fields[0])
	}
	if fields[1].Key != "ACTUAL_END_TIME" || fields[2].Key != "CLASSCODE" || fields[3].Key != "PRODH" {
		t.Fatalf("documented order not preserved: %+v", fields)
	}
}

func TestSerialBindingDetailFieldsUseBilingualLabelsAndOrder(t *testing.T) {
	doc := bson.M{
		"PRODH": "00100", "AUFNR_ITEM": "PO-ITEM", "ZCODE_HEAD": "HEAD-1", "ZCODE_ITEM": "ITEM-1", "AUFNR_HEAD": "PO-HEAD",
		"_id": "ignored",
	}
	fields := viewDetailFields("ZSGV_ZPP_SERNOLIST", doc)
	if len(fields) != 5 {
		t.Fatalf("fields=%+v", fields)
	}
	wantKeys := []string{"ZCODE_HEAD", "ZCODE_ITEM", "AUFNR_HEAD", "AUFNR_ITEM", "PRODH"}
	for index, wantKey := range wantKeys {
		if fields[index].Key != wantKey || fields[index].Label == wantKey {
			t.Fatalf("field %d=%+v, want translated %s", index, fields[index], wantKey)
		}
	}
	if fields[0].Label != "大刀/机头序列号（ZCODE_HEAD）" {
		t.Fatalf("unexpected label=%q", fields[0].Label)
	}
}

func TestBOMPostingDetailFieldsUseChineseLabelsAndOrder(t *testing.T) {
	if len(bomPostingFieldOrder) != len(bomPostingFieldLabels) {
		t.Fatalf("BOM field order and labels differ: %d != %d", len(bomPostingFieldOrder), len(bomPostingFieldLabels))
	}
	for _, key := range bomPostingFieldOrder {
		if bomPostingFieldLabels[key] == "" {
			t.Fatalf("missing Chinese label for BOM field %q", key)
		}
	}
	doc := bson.M{
		"VBELN_EX": "SO-1", "MENGE_A": "2", "MBLNR": "50000001", "MATNR": "MAT-1", "AUFNR_1": "PO-1",
		"_source_key": "bom-1", "_synced_at": "2026-09-03T00:00:00Z", "_id": "ignored",
	}
	fields := viewDetailFields("ZSGV_ZSD124", doc)
	if len(fields) != 7 {
		t.Fatalf("fields=%+v", fields)
	}
	want := []struct{ key, label string }{
		{"MATNR", "物料号"}, {"MENGE_A", "过账数量"}, {"AUFNR_1", "生产订单"}, {"VBELN_EX", "销售订单"},
		{"MBLNR", "物料凭证号"}, {"_source_key", "源记录键"}, {"_synced_at", "同步时间"},
	}
	for index, expected := range want {
		if fields[index].Key != expected.key || fields[index].Label != expected.label {
			t.Fatalf("field %d=%+v, want %+v", index, fields[index], expected)
		}
	}
}
