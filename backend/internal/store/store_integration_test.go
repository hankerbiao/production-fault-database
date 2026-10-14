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
		bson.M{"_id": "r2", "_source_key": "r2", "PCODE": "PC-2", "VBELN": "SO-1", "AUFNR": "PO-2", "ZDATE_WX": "20260101", "GSTRS": "2026-01-01", "MATNR": "B5000051", "RECORD01REPAIRM": "57000766", "ZTIME": "020000", "_synced_at": now},
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
	dbName = fmt.Sprintf("%s_summary_test_%d", dbName, time.Now().UnixNano())
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
	defer db.Drop(context.Background())
	sourceName := fmt.Sprintf("sales_order_delivery_source_%d", time.Now().UnixNano())
	source := db.Collection(sourceName)
	meta := db.Collection(salesOrderDeliverySummaryMetaCollection)
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
	var before bson.M
	if err := meta.FindOne(ctx, bson.M{"_id": "active"}).Decode(&before); err != nil {
		t.Fatal(err)
	}
	if err := s.RefreshSalesOrderDeliverySummaryIfStale(ctx); err != nil {
		t.Fatal(err)
	}
	var unchanged bson.M
	if err := meta.FindOne(ctx, bson.M{"_id": "active"}).Decode(&unchanged); err != nil {
		t.Fatal(err)
	}
	if before["collection"] != unchanged["collection"] {
		t.Fatal("unchanged source version rebuilt summary")
	}
	if _, err := source.InsertOne(ctx, bson.M{"_source_key": "cron-row", "VBELN": "SO-3", "ERDAT": "20260104"}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Collection("sync_dataset_versions").InsertOne(ctx, bson.M{"_id": "ZSGV_ZSD46N_SO_KK", "run_id": "cron-1"}); err != nil {
		t.Fatal(err)
	}
	if err := s.RefreshSalesOrderDeliverySummaryIfStale(ctx); err != nil {
		t.Fatal(err)
	}
	var refreshed bson.M
	if err := meta.FindOne(ctx, bson.M{"_id": "active"}).Decode(&refreshed); err != nil {
		t.Fatal(err)
	}
	if refreshed["source_version"] != "cron-1" || refreshed["collection"] == before["collection"] {
		t.Fatalf("cron version did not publish refreshed summary: %v", refreshed)
	}
	result, err = s.ViewList(ctx, "ZSGV_ZSD46N_SO_KK", ViewFilters{}, 1, 20, false)
	if err != nil || result.Total != 3 {
		t.Fatalf("cron summary total=%d err=%v", result.Total, err)
	}
	if _, err := db.Collection("sync_locks").InsertOne(ctx, bson.M{
		"_id": "dataset:" + sourceName, "expires_at": time.Now().Add(time.Minute),
	}); err != nil {
		t.Fatal(err)
	}
	if err := s.RefreshSalesOrderDeliverySummaryIfStale(ctx); err != nil {
		t.Fatal(err)
	}
	s.salesOrderDeliverySummaryStateMu.RLock()
	available := s.salesOrderDeliverySummaryAvailable
	s.salesOrderDeliverySummaryStateMu.RUnlock()
	if available {
		t.Fatal("active source writer did not disable materialized summary")
	}
	if _, err := db.Collection("sync_locks").DeleteOne(ctx, bson.M{"_id": "dataset:" + sourceName}); err != nil {
		t.Fatal(err)
	}
	if err := s.RefreshSalesOrderDeliverySummaryIfStale(ctx); err != nil {
		t.Fatal(err)
	}
}
