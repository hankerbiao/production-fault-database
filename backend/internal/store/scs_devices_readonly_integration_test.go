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

func TestSCSDevicesReadOnlyDatabase(t *testing.T) {
	uri, dbName := os.Getenv("SCS_READONLY_MONGO_URI"), os.Getenv("SCS_READONLY_MONGO_DB")
	if uri == "" || dbName == "" {
		t.Skip("set SCS_READONLY_MONGO_URI and SCS_READONLY_MONGO_DB for read-only check")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	c, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Disconnect(ctx)
	db := c.Database(dbName)
	s := &Store{views: map[string]*mongo.Collection{"SCS_DOA": db.Collection("scs_doa_records")}}
	result, err := s.SCSDevices(ctx, SCSDeviceFilters{}, 1, 100)
	if err != nil {
		t.Fatal(err)
	}
	count, err := db.Collection("scs_doa_records").CountDocuments(ctx, bson.M{"_prodh_scope": "00100"})
	if err != nil {
		t.Fatal(err)
	}
	if count == 0 && result.Total != 0 {
		t.Fatal("unscoped documents exposed")
	}
	t.Logf("scoped DOA documents=%d, accessible devices=%d", count, result.Total)
	// Read a legacy example only inside the test to validate the real nested
	// MongoDB projection; this never widens the endpoint's scope.
	var sample bson.M
	err = db.Collection("scs_doa_records").FindOne(ctx, bson.M{"details.equipment.product_fields.产品条码": bson.M{"$exists": true}}, options.FindOne().SetProjection(scsDeviceProjection())).Decode(&sample)
	if err == mongo.ErrNoDocuments {
		t.Skip("no legacy device example")
	}
	if err != nil {
		t.Fatal(err)
	}
	devices := groupSCSDevices([]bson.M{sample}, SCSDeviceFilters{})
	if len(devices) == 0 || devices[0].DeviceKey == "" || len(devices[0].Evidence) == 0 {
		t.Fatal("nested device projection did not decode")
	}
}
