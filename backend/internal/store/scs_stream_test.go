package store

import (
	"bytes"
	"context"
	"encoding/csv"
	"os"
	"slices"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func TestSCSStreamHostModel(t *testing.T) {
	device := func(model, legacy string) bson.M {
		return bson.M{"product_fields": bson.M{"主机型号": model}, "host_model": legacy}
	}
	tests := []struct {
		name      string
		equipment bson.A
		want      string
	}{
		{"primary before legacy", bson.A{device(" 6610 X2 ", "old model")}, "6610 X2"},
		{"legacy and repeated primary", bson.A{device(" ", "6610 X2"), device("6610 X2", ""), device("", "")}, "6610 X2"},
		{"missing", bson.A{device(" ", "")}, ""},
		{"no equipment", nil, ""},
		{"conflict", bson.A{device("6610 X2", ""), device("", "6610 X3")}, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			doc := bson.M{"details": bson.M{"equipment": tc.equipment}}
			if got := scsDOAStreamHostModel(doc); got != tc.want {
				t.Fatalf("host_model = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestSCSStreamHostModelReadOnlyDatabase(t *testing.T) {
	uri, dbName := os.Getenv("SCS_READONLY_MONGO_URI"), os.Getenv("SCS_READONLY_MONGO_DB")
	if uri == "" || dbName == "" {
		t.Skip("set SCS_READONLY_MONGO_URI and SCS_READONLY_MONGO_DB for read-only check")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	client, err := mongo.Connect(ctx, options.Client().ApplyURI(uri))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Disconnect(ctx)
	s := &Store{views: map[string]*mongo.Collection{"SCS_DOA": client.Database(dbName).Collection("scs_doa_records")}}
	var out bytes.Buffer
	if err := s.ViewSCSStream(ctx, "SCS_DOA", ViewFilters{ServiceOrder: "SH202610200254"}, &out); err != nil {
		t.Fatal(err)
	}
	r := csv.NewReader(&out)
	r.Comma = '\t'
	rows, err := r.ReadAll()
	if err != nil || len(rows) < 2 {
		t.Fatalf("TSV rows=%d err=%v", len(rows), err)
	}
	header := rows[0]
	model := slices.Index(header, "host_model")
	code := slices.Index(header, "doa_code")
	if model != len(header)-1 || code < 0 || slices.Contains(header, "details") {
		t.Fatalf("unexpected TSV header: %v", header)
	}
	for _, row := range rows[1:] {
		if row[code] == "KX202610310012" {
			if row[model] != "6610 X2" {
				t.Fatalf("SH202610200254 host_model = %q, want 6610 X2", row[model])
			}
			t.Logf("SH202610200254 host_model=%s", row[model])
			return
		}
	}
	t.Fatal("TSV missing expected DOA record KX202610310012")
}
