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
}

func TestRefreshSalesOrderDeliverySummaryWithMongo(t *testing.T) {
	uri := os.Getenv("MONGO_TEST_URI")
	if uri == "" {
		t.Skip("set MONGO_TEST_URI to run MongoDB integration tests")
	}
	dbName := os.Getenv("MONGO_TEST_DB")
	if dbName == "" {
		dbName = "production_fault_test"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Disconnect(context.Background())
	if err := client.Ping(ctx, nil); err != nil {
		t.Fatal(err)
	}

	db := client.Database(dbName)
	sourceName := fmt.Sprintf("sales_order_delivery_source_%d", time.Now().UnixNano())
	source := db.Collection(sourceName)
	meta := db.Collection(salesOrderDeliverySummaryMetaCollection)
	defer source.Drop(context.Background())
	defer meta.Drop(context.Background())
	defer func() {
		var active struct {
			Collection string `bson:"collection"`
		}
		if err := meta.FindOne(context.Background(), bson.M{"_id": "active"}).Decode(&active); err == nil && active.Collection != "" {
			_ = db.Collection(active.Collection).Drop(context.Background())
		}
	}()
	ctx, cancel = context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()

	_, err = source.InsertMany(ctx, []any{
		bson.M{"_source_key": "row-1", "VBELN": "SO-1", "POSNR": "10", "VBELN_L": "DL-1", "POSNR_L": "10", "ERDAT": "20260102", "KWMENG": "5", "LFIMG": "4", "SERNR": "SN-1"},
		bson.M{"_source_key": "row-2", "VBELN": "SO-1", "POSNR": "10", "VBELN_L": "DL-1", "POSNR_L": "10", "ERDAT": "20260102", "KWMENG": "5", "LFIMG": "4", "SERNR": "SN-2"},
		bson.M{"_source_key": "row-3", "VBELN": "SO-2", "POSNR": "20", "VBELN_L": "DL-2", "POSNR_L": "10", "ERDAT": "20260103", "KWMENG": "3", "LFIMG": "2", "SERNR": "SN-3"},
	})
	if err != nil {
		t.Fatal(err)
	}

	s := &Store{
		db:                             db,
		views:                          map[string]*mongo.Collection{"ZSGV_ZSD46N_SO_KK": source},
		salesOrderDeliverySummaryMeta:  meta,
		salesOrderDeliverySummaryReady: make(chan struct{}),
	}
	if err := s.RefreshSalesOrderDeliverySummary(ctx); err != nil {
		t.Fatal(err)
	}
	result, err := s.ViewList(ctx, "ZSGV_ZSD46N_SO_KK", ViewFilters{}, 1, 20, false)
	if err != nil {
		t.Fatal(err)
	}
	if result.Total != 2 || len(result.Items) != 2 {
		t.Fatalf("summary result total=%d items=%d, want 2 each", result.Total, len(result.Items))
	}
	if result.Items[0]["VBELN"] != "SO-2" || result.Items[1]["VBELN"] != "SO-1" {
		t.Fatalf("summary sort order=%v", []any{result.Items[0]["VBELN"], result.Items[1]["VBELN"]})
	}
	if result.Items[1]["KWMENG"] != float64(5) || result.Items[1]["serialCount"] != int32(2) && result.Items[1]["serialCount"] != int64(2) {
		t.Fatalf("sales order summary did not deduplicate quantities or retain serial count: %v", result.Items[1])
	}
}
