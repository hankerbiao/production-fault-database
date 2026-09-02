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

func TestOrderFilterAndMissingField(t *testing.T) {
	filter := orderFilter(OrderFilters{Source: "SG", GSTRSFrom: "2026-01-01", GSTRSTo: "2026-01-31", Keyword: "SO"})
	if len(filter["$and"].(primitive.A)) != 4 {
		t.Fatalf("filter=%v", filter)
	}
	missing := missingField("VBELN")
	if len(missing["$or"].(primitive.A)) != 3 {
		t.Fatalf("missing=%v", missing)
	}
}

func TestStatsUseSingleAggregationPipeline(t *testing.T) {
	for name, pipeline := range map[string]mongo.Pipeline{
		"repairs": repairStatsPipeline(repairFilter(Filters{HostBarcode: "PC-1"})),
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

func TestNormalizeFaultAndOrder(t *testing.T) {
	doc := bson.M{"_source_key": "key", "PCODE": " PC-1 ", "ZMCOD1": "SN-1", "ERROR_CODE": "E1", "ZDATE_WX": "20260102", "ZTIME": "030405", "VBELN": "SO1", "AUFNR": "PO1", "_synced_at": primitive.NewDateTimeFromTime(time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC))}
	fault := normalizeFault(doc)
	if fault.ID != "key" || fault.HostBarcode != "PC-1" || fault.RepairAt.IsZero() || fault.SalesOrder != "SO1" {
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
