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

func TestOrdersPaginationWithEqualDatesAndSyncTimes(t *testing.T) {
	uri := os.Getenv("MONGO_TEST_URI")
	if uri == "" {
		t.Skip("set MONGO_TEST_URI to run MongoDB integration tests")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Disconnect(context.Background())
	dbName := os.Getenv("MONGO_TEST_DB")
	if dbName == "" {
		dbName = "production_fault_test"
	}
	collection := client.Database(dbName).Collection(fmt.Sprintf("order_pagination_%d", time.Now().UnixNano()))
	defer collection.Drop(context.Background())

	const count = 205
	syncedAt := time.Date(2026, 10, 16, 2, 0, 0, 0, time.UTC)
	docs := make([]any, count)
	for index := range docs {
		docs[index] = bson.M{
			"_id": fmt.Sprintf("KK:%04d", index), "aufnr": fmt.Sprint(index),
			"data":           bson.M{"GSTRS": "2026-10-01", "WMENG": index + 1},
			"last_synced_at": syncedAt,
		}
	}
	if _, err := collection.InsertMany(ctx, docs); err != nil {
		t.Fatal(err)
	}
	s := &Store{orders: collection}
	seen := make(map[string]bool)
	var quantity float64
	const pageSize = 37
	for page := 1; (page-1)*pageSize < count; page++ {
		result, err := s.Orders(ctx, OrderFilters{}, page, pageSize)
		if err != nil {
			t.Fatal(err)
		}
		if result.Total != count {
			t.Fatalf("total=%d, want %d", result.Total, count)
		}
		for index, order := range result.Items {
			wantID := fmt.Sprintf("KK:%04d", count-1-((page-1)*pageSize+index))
			if order.ID != wantID {
				t.Fatalf("page=%d item=%d id=%s, want %s", page, index, order.ID, wantID)
			}
			if seen[order.ID] {
				t.Fatalf("order %s appears on multiple pages", order.ID)
			}
			seen[order.ID] = true
			quantity += order.StorageQuantity
		}
	}
	if len(seen) != count || quantity != count*(count+1)/2 {
		t.Fatalf("pagination lost orders or quantities: unique=%d quantity=%v", len(seen), quantity)
	}
	bulk, err := s.OrdersAll(ctx, OrderFilters{})
	if err != nil {
		t.Fatal(err)
	}
	for index, order := range bulk.Items {
		if wantID := fmt.Sprintf("KK:%04d", count-1-index); order.ID != wantID {
			t.Fatalf("bulk item=%d id=%s, want %s", index, order.ID, wantID)
		}
	}
}
