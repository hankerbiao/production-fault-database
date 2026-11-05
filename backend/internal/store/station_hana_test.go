package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"
	"net/url"
	"strings"
	"testing"

	"go.mongodb.org/mongo-driver/bson"
)

func TestStationHANADSNEnablesIdleConnectionHealthChecks(t *testing.T) {
	dsn := stationHANADSN("10.17.150.235", "30015", "report user", "p@ss", 120, 30)
	parsed, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Query().Get("timeout") != "120" || parsed.Query().Get("pingInterval") != "30" {
		t.Fatalf("dsn query=%v", parsed.Query())
	}
	if parsed.User.Username() != "report user" {
		t.Fatalf("dsn user=%q", parsed.User.Username())
	}
}

func TestRetryStationHANAQueryOnlyRetriesBadConnections(t *testing.T) {
	if !retryStationHANAQuery(context.Background(), errors.Join(driver.ErrBadConn, errors.New("reset")), 0) {
		t.Fatal("bad connection should be retried")
	}
	if retryStationHANAQuery(context.Background(), errors.New("syntax error"), 0) {
		t.Fatal("query errors must not be retried")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if retryStationHANAQuery(ctx, driver.ErrBadConn, 0) {
		t.Fatal("canceled query must not be retried")
	}
	if retryStationHANAQuery(context.Background(), driver.ErrBadConn, stationHANAQueryAttempts-1) {
		t.Fatal("retry limit must be one replay")
	}
}

type retryDriver struct{ queries *int }

func (d retryDriver) Open(string) (driver.Conn, error) { return retryConn{queries: d.queries}, nil }

type retryConn struct{ queries *int }

func (retryConn) Prepare(string) (driver.Stmt, error) { return nil, driver.ErrSkip }
func (retryConn) Close() error                        { return nil }
func (retryConn) Begin() (driver.Tx, error)           { return nil, driver.ErrSkip }
func (c retryConn) QueryContext(context.Context, string, []driver.NamedValue) (driver.Rows, error) {
	*c.queries++
	return &retryRows{bad: *c.queries == 1}, nil
}

type retryRows struct {
	bad, returned bool
}

func (*retryRows) Columns() []string { return []string{"PCODE"} }
func (*retryRows) Close() error      { return nil }
func (r *retryRows) Next(dest []driver.Value) error {
	if r.bad {
		r.bad = false
		return driver.ErrBadConn
	}
	if r.returned {
		return io.EOF
	}
	r.returned = true
	dest[0] = "PC-1"
	return nil
}

func TestStationHANAQueryEachRecoversBeforeFirstRow(t *testing.T) {
	queries := 0
	driverName := "station-retry-test"
	sql.Register(driverName, retryDriver{queries: &queries})
	db, err := sql.Open(driverName, "")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	h := &stationHANA{db: db}
	var values []string
	if err := h.queryEach(context.Background(), "SELECT PCODE", nil, func(doc map[string]any) error {
		values = append(values, stringValue(doc["PCODE"]))
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if queries != 2 || len(values) != 1 || values[0] != "PC-1" {
		t.Fatalf("queries=%d values=%v", queries, values)
	}
}

func TestStationStreamQueryKeepsCompatibleProjectionAndOrdering(t *testing.T) {
	query := stationStreamQuery(`WHERE "GSTRS" >= ?`, 123)
	for _, fragment := range []string{
		`SELECT ` + stationHANASelectList,
		`FROM ` + stationHANAView,
		`WHERE "GSTRS" >= ?`,
		`ORDER BY ` + stationHANAOrder,
		`LIMIT 123`,
	} {
		if !strings.Contains(query, fragment) {
			t.Fatalf("stream query misses %q: %s", fragment, query)
		}
	}
	page := stationStreamQueryPage(`WHERE "GSTRS" >= ?`, stationHANAStreamBatch, stationHANAStreamBatch)
	if !strings.Contains(page, "LIMIT 20 OFFSET 20") {
		t.Fatalf("paged stream query does not bound batch: %s", page)
	}
}

func TestStationStreamCountUsesDirectViewCount(t *testing.T) {
	where := `WHERE "GSTRS" >= ? AND "GSTRS" <= ?`
	query := `SELECT COUNT(*) FROM ` + stationHANAView
	if where != "" {
		query += " " + where
	}
	if strings.Contains(query, "LIMIT") || !strings.Contains(query, "FROM "+stationHANAView) {
		t.Fatalf("stream count query must use direct count: %s", query)
	}
}

func TestStationStreamCursorKeepsStableDescendingOrder(t *testing.T) {
	where, args := stationStreamCursorWhere(`WHERE "GSTRS" >= ?`, []any{"20261030"}, map[string]any{
		"ACTUAL_START_TIME": "2026-11-01 10:00:00", "HISTROYID": "H2", "SPEC_TIME": "2026-11-01 09:00:00",
		"PCODE": "P2", "SPEC": "S2", "OCODE": "O2",
	})
	if !strings.Contains(where, `"GSTRS" >= ? AND`) || !strings.Contains(where, `"ACTUAL_START_TIME" < ?`) || len(args) != 22 {
		t.Fatalf("where=%q args=%v", where, args)
	}
}

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
