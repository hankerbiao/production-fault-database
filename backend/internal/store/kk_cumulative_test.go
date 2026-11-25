package store

import (
	"context"
	"os"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func TestKKCumulativeBatchAggregation(t *testing.T) {
	uri := os.Getenv("MONGO_TEST_URI")
	if uri == "" {
		t.Skip("set MONGO_TEST_URI to run the aggregation against MongoDB")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Disconnect(context.Background())

	row := func(delivery, order, item, date, sn string, qty any) bson.M {
		doc := bson.M{"VBELN_L": delivery, "VBELN": order, "POSNR": item,
			"MATNR": "SAME-MATERIAL", "WADAT": date, "SN": sn}
		if qty != nil {
			doc["LFIMG"] = qty
		}
		return doc
	}
	pipeline := mongo.Pipeline{{{Key: "$documents", Value: bson.A{
		row("D1", "SO1", "10", "20261001", "S1", 2),
		row("D1", "SO1", "10", "20261001", "S2", 2),
		row("D1", "SO1", "10", "20261102", "S2", 2), // same batch stays in October
		row("D1", "SO1", "10", "20261001", "S1", 3), // split quantity on same line
		row("D1", "SO1", "20", "20261001", "S3", 2), // separate line with same material and quantity
		row("D2", "SO1", "10", "20261010", " S1 ", nil),
		row("D2", "SO1", "10", "20261010", "S1", nil),
		row("D3", "SO1", "10", "20261105", "S1", 0), // count S1 in November too
		row("D4", "SO2", "10", "20261106", "", 4),
		row("D5", "SO1", "10", "20260230", "S4", 6), // invalid calendar date
		row("D6", "SO1", "10", "", "S5", 7),
		row("D7", "SO1", "10", "20261126", "S6", 8), // after cutoff
	}}}}
	pipeline = append(pipeline, kkCumulativePipeline(bson.M{"WADAT": bson.M{"$lte": "20261125"}})...)
	// A database-level $documents pipeline exercises the real MongoDB operators without writing test rows.
	cursor, err := client.Database("admin").Aggregate(ctx, pipeline)
	if err != nil {
		t.Fatal(err)
	}
	var results []struct {
		ID struct {
			SalesOrder string `bson:"salesOrder"`
			Month      string `bson:"month"`
		} `bson:"_id"`
		Devices  int     `bson:"devices"`
		Quantity float64 `bson:"quantity"`
		Batches  int     `bson:"batchCount"`
		Missing  int     `bson:"missingQuantityBatches"`
	}
	if err := cursor.All(ctx, &results); err != nil {
		t.Fatal(err)
	}
	type group struct{ Order, Month string }
	type counts struct {
		Quantity                  float64
		Devices, Batches, Missing int
	}
	want := map[group]counts{
		{"SO1", "2026-10"}: {8, 3, 4, 1},
		{"SO1", "2026-11"}: {1, 1, 1, 1},
		{"SO2", "2026-11"}: {4, 0, 1, 0},
		{"SO1", ""}:        {13, 2, 2, 0},
	}
	if len(results) != len(want) {
		t.Fatalf("unexpected groups: %+v", results)
	}
	for _, got := range results {
		key := group{got.ID.SalesOrder, got.ID.Month}
		if expected, ok := want[key]; !ok || expected != (counts{got.Quantity, got.Devices, got.Batches, got.Missing}) {
			t.Errorf("group %v: got %+v, want %+v", key, got, expected)
		}
		delete(want, key)
	}
}
