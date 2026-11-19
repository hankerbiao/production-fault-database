package store

import (
	"testing"

	"go.mongodb.org/mongo-driver/bson"
)

func TestStationNormalizedOrderFilter(t *testing.T) {
	t.Setenv("STATION_NORMALIZED_ORDERS", "true")
	filter := viewFilter("Z_V_ZMES_T_001", ViewFilters{ProductionOrder: "000123,0000"}, nil, "")
	clauses := filter["$and"].(bson.A)
	values := clauses[0].(bson.M)["AUFNR_NORMALIZED"].(bson.M)["$in"].([]string)
	if len(values) != 2 || values[0] != "123" || values[1] != "0" {
		t.Fatalf("unexpected normalized orders: %v", values)
	}
	filter = viewFilter("Z_V_ZMES_T_001", ViewFilters{ProductionOrder: "ABC"}, nil, "")
	clauses = filter["$and"].(bson.A)
	if _, ok := clauses[0].(bson.M)["AUFNR"]; !ok {
		t.Fatalf("nonnumeric order must use legacy filter: %v", filter)
	}
}
