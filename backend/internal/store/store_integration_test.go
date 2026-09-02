package store

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func TestMongoQueriesWithTemporaryDatabase(t *testing.T) {
	uri := os.Getenv("MONGO_TEST_URI")
	if uri == "" {
		t.Skip("set MONGO_TEST_URI to run MongoDB integration tests")
	}
	dbName := os.Getenv("MONGO_TEST_DB")
	if dbName == "" {
		dbName = "production_fault_test"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Disconnect(context.Background())
	if err := client.Ping(ctx, nil); err != nil {
		t.Fatal(err)
	}
	repairName := fmt.Sprintf("repairs_%d", time.Now().UnixNano())
	orderName := fmt.Sprintf("orders_%d", time.Now().UnixNano())
	db := client.Database(dbName)
	defer db.Collection(repairName).Drop(context.Background())
	defer db.Collection(orderName).Drop(context.Background())
	now := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	_, err = db.Collection(repairName).InsertMany(ctx, []any{
		bson.M{"_id": "r1", "_source_key": "r1", "PCODE": "PC-1", "VBELN": "SO-1", "AUFNR": "PO-1", "ERROR_CODE": "E1", "ZDATE_WX": "20260102", "ZTIME": "030405", "_synced_at": now},
		bson.M{"_id": "r2", "_source_key": "r2", "PCODE": "PC-2", "VBELN": "SO-1", "AUFNR": "PO-2", "ZDATE_WX": "20260101", "ZTIME": "020000", "_synced_at": now},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Collection(orderName).InsertMany(ctx, []any{
		bson.M{"_id": "SG:PO-1", "source": "SG", "aufnr": "PO-1", "data": bson.M{"VBELN": "SO-1", "GSTRS": "2026-01-02", "GAMNG": "3", "WMENG": "2"}, "order_quantity": 3.0, "storage_quantity": 2.0, "record_count": 1, "last_synced_at": now},
		bson.M{"_id": "KK:PO-2", "source": "KK", "aufnr": "PO-2", "data": bson.M{"VBELN": "SO-1", "GSTRS": "2026-01-03", "GAMNG": "4", "WMENG": "1"}, "order_quantity": 4.0, "storage_quantity": 1.0, "record_count": 1, "last_synced_at": now},
	})
	if err != nil {
		t.Fatal(err)
	}

	s, err := New(ctx, uri, dbName, repairName, orderName)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close(context.Background())
	list, err := s.List(ctx, Filters{Keyword: "E1"}, 1, 1)
	if err != nil || list.Total != 1 || len(list.Items) != 1 || list.Items[0].ID != "r1" {
		t.Fatalf("list=%+v err=%v", list, err)
	}
	stats, err := s.Stats(ctx, Filters{})
	if err != nil || stats.Total != 2 || stats.SalesOrders != 1 || stats.ProductionOrders != 2 {
		t.Fatalf("stats=%+v err=%v", stats, err)
	}
	orderStats, err := s.OrderStats(ctx, OrderFilters{Source: "SG"})
	if err != nil || orderStats.Total != 1 || orderStats.SalesOrders != 1 || orderStats.SG != 1 || orderStats.OrderQuantity != 3 {
		t.Fatalf("orderStats=%+v err=%v", orderStats, err)
	}
}
