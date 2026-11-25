package store

import (
	"testing"

	"go.mongodb.org/mongo-driver/bson"
)

func TestSummarizeKKShipmentsSumsBatchQuantityOnce(t *testing.T) {
	batches := []kkShipmentBatch{
		// One delivery line of 300 units: LFIMG is repeated on all 300 rows, but
		// the batch is counted once.
		{Serials: []string{"9800000000000001"}, SalesOrder: "XNC2648220", Quantity: 300, DeliveryAt: "20261005"},
		// Same sales order, second batch in the same month.
		{Serials: []string{"9800000000000002"}, SalesOrder: "XNC2648220", Quantity: 139, DeliveryAt: "20261020"},
		// No sales order: falls into the unknown-model bucket.
		{Serials: []string{"9800000000000003"}, SalesOrder: "", Quantity: 5, DeliveryAt: "20261011"},
	}
	models := []string{"6215 C2", "6215 C2", ""}

	stats := summarizeKKShipments(batches, models, KKShipmentStats{})

	if stats.Quantity != 444 {
		t.Fatalf("quantity=%v, want 444", stats.Quantity)
	}
	if stats.BatchCount != 3 {
		t.Fatalf("batches=%d, want 3", stats.BatchCount)
	}
	if stats.SalesOrderCount != 1 {
		t.Fatalf("sales orders=%d, want 1", stats.SalesOrderCount)
	}
	if stats.UnknownModel != 5 {
		t.Fatalf("unknown model=%d, want 5", stats.UnknownModel)
	}
	if len(stats.ByMonth) != 1 || stats.ByMonth[0].Month != "2026-10" || stats.ByMonth[0].Quantity != 444 {
		t.Fatalf("by month=%+v", stats.ByMonth)
	}
	if len(stats.ByModel) != 2 || stats.ByModel[0].Model != "6215 C2" || stats.ByModel[0].Quantity != 439 {
		t.Fatalf("by model=%+v", stats.ByModel)
	}
	if stats.ByModel[1].Model != KKUnknownModel || stats.ByModel[1].Quantity != 5 {
		t.Fatalf("by model=%+v", stats.ByModel)
	}
}

func TestSummarizeKKShipmentsKeepsUndatedUnitsInTotal(t *testing.T) {
	batches := []kkShipmentBatch{{Serials: []string{"9800000000000004", "9800000000000005"}, SalesOrder: "XNC1", Quantity: 2}}
	stats := summarizeKKShipments(batches, []string{"6410 X2"}, KKShipmentStats{})

	if stats.Quantity != 2 || len(stats.ByMonth) != 0 || stats.UndatedDevices != 2 {
		t.Fatalf("stats=%+v", stats)
	}
	if stats.UnknownModel != 0 || len(stats.ByModel) != 1 || stats.ByModel[0].Model != "6410 X2" {
		t.Fatalf("by model=%+v", stats.ByModel)
	}
}

func TestShipmentBreakdownsDeduplicateSerialsWithinEachBucket(t *testing.T) {
	batches := []kkShipmentBatch{
		{Quantity: 1, Serials: []string{"SN1"}, DeliveryAt: "20261001"},
		{Quantity: 2, Serials: []string{"SN1", "SN2"}, DeliveryAt: "20261002"},
		{Quantity: 1, Serials: []string{"SN1"}, DeliveryAt: "20261101"},
	}
	stats := summarizeKKShipments(batches, []string{"A", "A", "A"}, KKShipmentStats{})
	if stats.Quantity != 4 || stats.ByModel[0].Devices != 2 ||
		stats.ByMonth[0].Devices != 2 || stats.ByMonth[1].Devices != 1 {
		t.Fatalf("unexpected quantity or distinct serial counts: %+v", stats)
	}
}

func TestKKFilterWithAppendsInsteadOfNesting(t *testing.T) {
	filter := kkDeliveryFilter(KKDeliveryFilters{DateFrom: "2026-10-01", DateTo: "2026-10-31"})
	merged := kkFilterWith(filter, bson.M{"SN": bson.M{"$nin": bson.A{nil, ""}}})

	conditions, ok := merged["$and"].(bson.A)
	if !ok || len(conditions) != 2 {
		t.Fatalf("merged filter=%#v", merged)
	}
}
