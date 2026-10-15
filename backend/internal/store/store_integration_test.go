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
		bson.M{"_id": "r1", "_source_key": "r1", "_legacy_ids": bson.A{"old-source-key", "merged-document-id"}, "PCODE": "PC-1", "VBELN": "SO-1", "AUFNR": "PO-1", "ERROR_CODE": "E1", "ZDATE_WX": "20260102", "ZTIME": "030405", "_synced_at": now},
		bson.M{"_id": "r2", "_source_key": "r2", "PCODE": "PC-2", "VBELN": "SO-1", "AUFNR": "PO-UNLISTED", "ZDATE_WX": "20260101", "GSTRS": "2026-01-01", "MATNR": "B5000051", "RECORD01REPAIRM": "57000766", "ZTIME": "020000", "_synced_at": now},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Collection(orderName).InsertMany(ctx, []any{
		bson.M{"_id": "SG:PO-1", "source": "SG", "aufnr": "PO-1", "data": bson.M{"VBELN": "SO-1", "MAKTX": "Model-ww-Overseas", "GSTRS": "2026-01-02", "GAMNG": "3", "WMENG": "2"}, "order_quantity": 3.0, "storage_quantity": 2.0, "record_count": 1, "last_synced_at": now},
		bson.M{"_id": "KK:PO-2", "source": "KK", "aufnr": "PO-2", "data": bson.M{"VBELN": "SO-1", "MAKTX": "Domestic", "MAKTX_TH": "Model-WW", "GSTRS": "2026-01-03", "GAMNG": "4", "WMENG": "1"}, "order_quantity": 4.0, "storage_quantity": 1.0, "record_count": 1, "last_synced_at": now},
	})
	if err != nil {
		t.Fatal(err)
	}

	s, err := New(ctx, uri, dbName, repairName, orderName)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close(context.Background())
	repairs, err := s.FaultRowsBySNS(ctx, []string{"PC-2"}, "2026-01-01", "2026-01-31", "", "planned")
	if err != nil || len(repairs) != 1 || repairs[0]["MATNR"] != "B5000051" || repairs[0]["RECORD01REPAIRM"] != "57000766" {
		t.Fatalf("bulk repairs lose LRR material fields: rows=%v err=%v", repairs, err)
	}
	for _, id := range []string{"r1", "old-source-key", "merged-document-id"} {
		detail, err := s.FaultDetail(ctx, id)
		if err != nil || detail.Fault.ID != "r1" {
			t.Fatalf("detail id=%s result=%+v err=%v", id, detail, err)
		}
	}
	list, err := s.List(ctx, Filters{Keyword: "E1"}, 1, 1)
	if err != nil || list.Total != 1 || len(list.Items) != 1 || list.Items[0].ID != "r1" {
		t.Fatalf("list=%+v err=%v", list, err)
	}
	companyList, err := s.List(ctx, Filters{Company5000: "yes"}, 1, 10)
	if err != nil || companyList.Total != 1 || len(companyList.Items) != 1 || companyList.Items[0].ID != "r1" {
		t.Fatalf("5000 list=%+v err=%v", companyList, err)
	}
	nonCompanyList, err := s.List(ctx, Filters{Company5000: "no"}, 1, 10)
	if err != nil || nonCompanyList.Total != 1 || len(nonCompanyList.Items) != 1 || nonCompanyList.Items[0].ID != "r2" {
		t.Fatalf("non-5000 list=%+v err=%v", nonCompanyList, err)
	}
	stats, err := s.Stats(ctx, Filters{})
	if err != nil || stats.Total != 2 || stats.SalesOrders != 1 || stats.ProductionOrders != 2 {
		t.Fatalf("stats=%+v err=%v", stats, err)
	}
	companyStats, err := s.Stats(ctx, Filters{Company5000: "yes"})
	if err != nil || companyStats.Total != 1 || companyStats.ProductionOrders != 1 {
		t.Fatalf("5000 stats=%+v err=%v", companyStats, err)
	}
	orderStats, err := s.OrderStats(ctx, OrderFilters{Source: "SG"})
	if err != nil || orderStats.Total != 1 || orderStats.SalesOrders != 1 || orderStats.SG != 1 || orderStats.OrderQuantity != 3 {
		t.Fatalf("orderStats=%+v err=%v", orderStats, err)
	}
	unfiltered, err := s.Orders(ctx, OrderFilters{}, 1, 1)
	if err != nil || unfiltered.Total != 2 || len(unfiltered.Items) != 1 || unfiltered.Items[0].ID != "KK:PO-2" {
		t.Fatalf("unfiltered first page=%+v err=%v", unfiltered, err)
	}
	overseas, err := s.Orders(ctx, OrderFilters{Overseas: true}, 1, 1)
	if err != nil || overseas.Total != 1 || len(overseas.Items) != 1 || overseas.Items[0].ID != "SG:PO-1" {
		t.Fatalf("overseas search must match descriptions outside the unfiltered first page: %+v err=%v", overseas, err)
	}
	overseasStats, err := s.OrderStats(ctx, OrderFilters{Overseas: true})
	if err != nil || overseasStats.Total != 1 || overseasStats.OrderQuantity != 3 || overseasStats.StorageQuantity != 2 {
		t.Fatalf("overseas stats=%+v err=%v", overseasStats, err)
	}
	allOverseas, err := s.OrdersAll(ctx, OrderFilters{Overseas: true})
	if err != nil || allOverseas.Total != 1 || len(allOverseas.Items) != 1 || allOverseas.Items[0].ID != "SG:PO-1" {
		t.Fatalf("all overseas=%+v err=%v", allOverseas, err)
	}
	combined, err := s.Orders(ctx, OrderFilters{Overseas: true, Source: "KK"}, 1, 20)
	if err != nil || combined.Total != 0 || len(combined.Items) != 0 {
		t.Fatalf("overseas filter must intersect with other conditions: %+v err=%v", combined, err)
	}
}
