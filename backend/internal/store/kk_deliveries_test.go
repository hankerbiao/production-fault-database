package store

import (
	"testing"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
)

func TestKKDeliveryFilterUsesCompactDatesAndPreservesIdentifiers(t *testing.T) {
	filter := kkDeliveryFilter(KKDeliveryFilters{
		DateFrom: "2026-11-01", DateTo: "2026-11-30", SalesOrder: "0000123456",
		MaterialNumber: "0000000061234", CustomerNumber: "0000009876", Plant: "1000", SN: "612340123456789",
	})
	conditions, ok := filter["$and"].(bson.A)
	if !ok || len(conditions) != 6 {
		t.Fatalf("expected six filter clauses, got %#v", filter)
	}
	got := map[string]any{}
	for _, clause := range conditions {
		for field, value := range clause.(bson.M) {
			got[field] = value
		}
	}
	if got["WADAT"].(bson.M)["$gte"] != "20261101" || got["WADAT"].(bson.M)["$lte"] != "20261130" {
		t.Fatalf("unexpected date range: %#v", got["WADAT"])
	}
	if got["VBELN"] != "0000123456" || got["KUNNR"] != "0000009876" || got["WERKS"] != "1000" || got["SN"] != "612340123456789" {
		t.Fatalf("identifier strings were changed: %#v", got)
	}
	candidates := got["MATNR"].(bson.M)["$in"].([]string)
	seen := map[string]bool{}
	for _, candidate := range candidates {
		seen[candidate] = true
	}
	if !seen["0000000061234"] || !seen["61234"] {
		t.Fatalf("material candidates should include SAP-padded and normalized values: %#v", candidates)
	}
}

func TestKKDeliveryFilterMatchesSNExactly(t *testing.T) {
	filter := kkDeliveryFilter(KKDeliveryFilters{SN: "SN-000123"})
	conditions := filter["$and"].(bson.A)
	if len(conditions) != 1 || conditions[0].(bson.M)["SN"] != "SN-000123" {
		t.Fatalf("expected exact SN predicate, got %#v", filter)
	}
}

func TestKKDeliveryDecodesMissingAndNullSerialFields(t *testing.T) {
	for name, source := range map[string]bson.M{
		"legacy": {"VBELN": "0000123456", "MATNR": "0000000061234", "WADAT": "20261124", "KUNNR": "0000009876", "WERKS": "1000"},
		"null":   {"VBELN": "0000123456", "MATNR": "0000000061234", "WADAT": "20261124", "KUNNR": "0000009876", "WERKS": "1000", "SERNR": nil, "SN": nil},
	} {
		t.Run(name, func(t *testing.T) {
			encoded, err := bson.Marshal(source)
			if err != nil {
				t.Fatal(err)
			}
			var item KKDelivery
			if err := bson.Unmarshal(encoded, &item); err != nil {
				t.Fatal(err)
			}
			if item.DeviceSerial != "" || item.SN != "" {
				t.Fatalf("missing/null serial fields should decode as empty strings: %+v", item)
			}
		})
	}
}

func TestKKDeliveryPreservesSerialNumberStrings(t *testing.T) {
	encoded, err := bson.Marshal(bson.M{"SERNR": "0001234", "SN": "612340123456789"})
	if err != nil {
		t.Fatal(err)
	}
	var item KKDelivery
	if err := bson.Unmarshal(encoded, &item); err != nil {
		t.Fatal(err)
	}
	if item.DeviceSerial != "0001234" || item.SN != "612340123456789" {
		t.Fatalf("serial strings changed during decode: %+v", item)
	}
}

func TestKKDeliveryFilterWithoutInputsIsEmpty(t *testing.T) {
	if filter := kkDeliveryFilter(KKDeliveryFilters{}); len(filter) != 0 {
		t.Fatalf("expected an empty filter, got %#v", filter)
	}
}

func TestKKDeliveryDedupPipelineKeepsLatestDATUMPerSN(t *testing.T) {
	pipeline := kkDeliveryDedupPipeline(bson.M{"SN": "SN-1"})
	if len(pipeline) != 6 {
		t.Fatalf("pipeline stage count=%d, want 6: %#v", len(pipeline), pipeline)
	}
	if pipeline[0][0].Key != "$match" {
		t.Fatalf("first stage should apply filters: %#v", pipeline[0])
	}
	sortStage := pipeline[1][0].Value.(bson.D)
	if sortStage[0].Key != "DATUM" || sortStage[0].Value != -1 {
		t.Fatalf("records must sort by latest DATUM first: %#v", sortStage)
	}
	if pipeline[2][0].Key != "$project" {
		t.Fatalf("documents should be projected before grouping: %#v", pipeline[2])
	}
	group := pipeline[3][0].Value.(bson.D)
	if group[0].Key != "_id" || group[1].Key != "record" {
		t.Fatalf("expected grouped record selected with $first: %#v", group)
	}
	first := group[1].Value.(bson.D)
	if first[0].Value != "$$ROOT" {
		t.Fatalf("dedup should select the complete latest record: %#v", first)
	}
}

func TestKKDeliveryGroupKeyKeepsBlankSNRowsSeparate(t *testing.T) {
	key := kkDeliveryGroupKey()
	if _, ok := key["sn"]; !ok {
		t.Fatalf("group key should contain SN: %#v", key)
	}
	row := key["row"].(bson.M)["$cond"].(bson.A)
	if row[1] != "$_id" {
		t.Fatalf("blank SN rows should group by their own id: %#v", row)
	}
}

func TestKKDeliveryPagePipelineUsesBoundedLookup(t *testing.T) {
	pipeline := kkDeliveryPagePipeline(bson.M{"SN": "SN-1"}, "deliveries", 2, 20)
	if len(pipeline) != 3 {
		t.Fatalf("pipeline stage count=%d, want 3: %#v", len(pipeline), pipeline)
	}
	if pipeline[0][0].Key != "$match" {
		t.Fatalf("first stage should apply filters: %#v", pipeline[0])
	}
	group := pipeline[1][0].Value.(bson.D)
	latest := group[1].Value.(bson.D)
	maxValue := latest[0].Value.(bson.D)
	if maxValue[0].Key != "DATUM" || maxValue[1].Key != "_id" {
		t.Fatalf("latest marker must order by DATUM then _id: %#v", maxValue)
	}
	facet := pipeline[2][0].Value.(bson.D)
	if facet[0].Key != "items" || facet[1].Key != "total" {
		t.Fatalf("facet should return only page items and total: %#v", facet)
	}
	items := facet[0].Value.(mongo.Pipeline)
	if items[1][0].Value != int64(20) {
		t.Fatalf("page skip/limit must match page 2 size 20: %#v", items)
	}
	lookup := items[3][0].Value.(bson.D)
	if lookup[0].Key != "from" || lookup[0].Value != "deliveries" {
		t.Fatalf("lookup must read from the active collection: %#v", lookup)
	}
}
