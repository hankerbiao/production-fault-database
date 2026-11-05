package store

import (
	"strings"
	"testing"

	"go.mongodb.org/mongo-driver/bson"
)

func TestStationSourceKeyMatchesExistingCompleteKey(t *testing.T) {
	doc := bson.M{
		"HISTROYID": "0009888000005393",
		"SPEC":      "M-OP150",
		"SPEC_TIME": "2026-08-20 11:05:17",
		"PCODE":     "0231AP3G03021425",
		"OCODE":     "210231AP3GXXXXXXXXXX",
	}
	wantKey := `["0009888000005393","M-OP150","2026-08-20 11:05:17","0231AP3G03021425","210231AP3GXXXXXXXXXX"]`
	if got := stationSourceKey(doc); got != wantKey {
		t.Fatalf("source key=%q, want %q", got, wantKey)
	}
	if got := stationDocumentID(wantKey); got != "b22559f93cc78fe3997d4f593a432d49aee503c52be4a6b30b12b3a30a0caad9" {
		t.Fatalf("document id=%q", got)
	}
}

func TestStationSourceKeyKeepsEmptyKeyFieldsQueryable(t *testing.T) {
	doc := bson.M{
		"HISTROYID": "0009888005a6899f",
		"SPEC":      "TYM-OP210",
		"SPEC_TIME": "2026-10-21 12:55:08",
		"PCODE":     "9800228904705312",
		"OCODE":     "",
	}
	wantKey := `["0009888005a6899f","TYM-OP210","2026-10-21 12:55:08","9800228904705312",""]`
	if got := stationSourceKey(doc); got != wantKey {
		t.Fatalf("source key=%q, want %q", got, wantKey)
	}
	where, args, ok := stationSourceKeyWhere(wantKey)
	if !ok || len(args) != 5 || args[4] != "" {
		t.Fatalf("where=%q args=%v ok=%t", where, args, ok)
	}
	if !strings.Contains(where, `TRIM(COALESCE("OCODE", '')) = ?`) {
		t.Fatalf("where does not normalize empty OCODE: %q", where)
	}
}

func TestStationWhereTranslatesDocumentedFilters(t *testing.T) {
	where, args := stationWhere(ViewFilters{
		DateFrom:        "2026-10-20",
		DateTo:          "2026-10-20",
		ProductModel:    "TL300F-B",
		Keyword:         "ABC",
		ProductionOrder: "00030237510",
		StationCode:     "TYM-OP210",
		Base:            "7000",
		SN:              "9800228904705312",
	})
	for _, fragment := range []string{
		`"GSTRS" >= ?`,
		`"GSTRS" <= ?`,
		`"MAKTX_TH" = ?`,
		`LOWER(COALESCE("HISTROYID", '')) LIKE ? ESCAPE '\'`,
		`UPPER("AUFNR") = UPPER(?)`,
		`LTRIM("AUFNR", '0')`,
		`("PCODE" = ? OR "SPEC" = ? OR "OPERATION" = ?)`,
		`"LGORT" = ?`,
		`UPPER("PCODE") = UPPER(?)`,
	} {
		if !strings.Contains(where, fragment) {
			t.Fatalf("where misses %q: %s", fragment, where)
		}
	}
	for _, value := range []string{"20261020", "TL300F-B", "%abc%", "00030237510", "30237510", "TYM-OP210", "7000", "9800228904705312"} {
		if !containsAny(args, value) {
			t.Fatalf("args miss %q: %v", value, args)
		}
	}
}

func TestStationWhereWithoutFiltersIsEmpty(t *testing.T) {
	where, args := stationWhere(ViewFilters{})
	if where != "" || len(args) != 0 {
		t.Fatalf("where=%q args=%v", where, args)
	}
}

func TestStationSelectQueryIncludesStableOrderAndPaging(t *testing.T) {
	query := stationSelectQuery(`WHERE "GSTRS" = ?`, stationHANAOrder, 2, 4)
	for _, fragment := range []string{`FROM ` + stationHANAView, `ORDER BY ` + stationHANAOrder, `LIMIT 2`, `OFFSET 4`} {
		if !strings.Contains(query, fragment) {
			t.Fatalf("query misses %q: %s", fragment, query)
		}
	}
}

func TestStationCodeFromValues(t *testing.T) {
	cases := map[string]string{
		"M-OP310":      "OP310",
		"OP300":        "OP300",
		"M-OP310:过站":   "OP310",
		"LINE-1-OP310": "OP310",
		"TYM-OP210":    "OP210",
	}
	for input, want := range cases {
		if got := stationCodeFromValues("", input, ""); got != want {
			t.Fatalf("station code for %q=%q, want %q", input, got, want)
		}
	}
	if got := stationCodeFromValues("", "", "OP300"); got != "OP300" {
		t.Fatalf("operation fallback=%q", got)
	}
}

func containsAny(values []any, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
