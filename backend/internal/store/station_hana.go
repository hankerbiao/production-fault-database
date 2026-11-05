package store

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"database/sql/driver"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	_ "github.com/SAP/go-hdb/driver"
	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
)

const (
	stationHANAView          = `"_SYS_BIC"."BW_LOCAL.ZTRRI/Z_V_ZMES_T_001"`
	stationHANAViewID        = "Z_V_ZMES_T_001"
	stationHANASelectList    = `"MANDT","HISTROYID","PCODE","OCODE","AUFNR","KDAUF","KDPOS","LGORT","MAKTX_TH","LEAD_CYCLE","AUFNR_CYCLE","CUSTOMIZE_CYCLE","OEMBZ","SPEC","OPERATION","SPEC_DESC","UNAME","LASTSPEC_TIME","SPEC_TIME","GSTRS","PLAN_END_TIME","ACTUAL_START_TIME","ACTUAL_END_TIME","HADE1","HADE2","HADE3","HADE4","HADE5","HADE6","HADE7","HADE8","STATU","MESS"`
	stationHANAOrder         = `"ACTUAL_START_TIME" DESC, "HISTROYID" DESC, "SPEC_TIME" DESC, "PCODE" DESC, "SPEC" DESC, "OCODE" DESC`
	stationHANAQueryAttempts = 5
	stationHANAStreamBatch   = 10
)

var stationHANAKeyFields = []string{"HISTROYID", "SPEC", "SPEC_TIME", "PCODE", "OCODE"}

var stationHANAKeywordFields = []string{
	"HISTROYID", "PCODE", "OCODE", "AUFNR", "SPEC", "OPERATION", "GSTRS", "ACTUAL_START_TIME", "ACTUAL_END_TIME",
}

type stationHANA struct {
	db *sql.DB
}

func stationHANADSN(address, port, user, password string, timeout, pingInterval int) string {
	dsn := &url.URL{
		Scheme: "hdb",
		User:   url.UserPassword(user, password),
		Host:   net.JoinHostPort(address, port),
	}
	query := dsn.Query()
	query.Set("timeout", strconv.Itoa(timeout))
	if pingInterval > 0 {
		query.Set("pingInterval", strconv.Itoa(pingInterval))
	}
	dsn.RawQuery = query.Encode()
	return dsn.String()
}

func newStationHANAFromEnv() (*stationHANA, error) {
	address := strings.TrimSpace(os.Getenv("HANA_ADDRESS"))
	user := strings.TrimSpace(os.Getenv("HANA_USER"))
	password := os.Getenv("HANA_PASSWORD")
	if address == "" || user == "" || password == "" {
		return nil, fmt.Errorf("缺少 HANA 配置: 需要 HANA_ADDRESS、HANA_USER、HANA_PASSWORD")
	}
	port := strings.TrimSpace(os.Getenv("HANA_PORT"))
	if port == "" {
		port = "30015"
	}
	timeout := envSeconds("HANA_CONNECT_TIMEOUT", 120)
	// HANA or an intermediate proxy can close idle TCP sessions. Ask go-hdb to
	// validate an idle pooled session before handing it to a query so database/sql
	// can discard it and transparently establish a fresh connection.
	pingInterval := envSeconds("HANA_PING_INTERVAL", 30)
	db, err := sql.Open("hdb", stationHANADSN(address, port, user, password, timeout, pingInterval))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(12)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(25 * time.Minute)
	db.SetConnMaxIdleTime(5 * time.Minute)
	return &stationHANA{db: db}, nil
}

func envSeconds(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value < 1 {
		return fallback
	}
	return value
}

func (h *stationHANA) close() error {
	if h == nil || h.db == nil {
		return nil
	}
	return h.db.Close()
}

func stationViewSource() string {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("STATION_VIEW_SOURCE")), "mongo") {
		return "mongo"
	}
	return "hana"
}

func (s *Store) stationHANAReader() (*stationHANA, error) {
	if s.stationHANA != nil {
		return s.stationHANA, nil
	}
	if s.stationErr != nil {
		return nil, fmt.Errorf("工位 HANA 数据源不可用: %w", s.stationErr)
	}
	return nil, fmt.Errorf("工位 HANA 数据源未初始化")
}

func (s *Store) stationViewList(ctx context.Context, f ViewFilters, page, pageSize int, preview, includeTotal bool) (ViewListResult, error) {
	h, err := s.stationHANAReader()
	if err != nil {
		return ViewListResult{}, err
	}
	where, args := stationWhere(f)
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 20
	}
	offset := (page - 1) * pageSize
	query := stationSelectQuery(where, stationHANAOrder, pageSize, offset)
	now := time.Now().UTC().Format(time.RFC3339Nano)

	if preview {
		docs, err := h.query(ctx, stationSelectQuery(where, stationHANAOrder, pageSize+1, offset), args...)
		if err != nil {
			return ViewListResult{}, err
		}
		hasMore := len(docs) > pageSize
		if hasMore {
			docs = docs[:pageSize]
		}
		for _, doc := range docs {
			normalizeStationHANAView(doc, now)
		}
		return ViewListResult{Items: docs, Page: page, PageSize: pageSize, Total: int64(len(docs)), HasMore: hasMore, Preview: true}, nil
	}

	var total int64
	var docs []bson.M
	var countErr, findErr error
	if includeTotal {
		done := make(chan struct{})
		go func() {
			defer close(done)
			total, countErr = h.count(ctx, where, args...)
		}()
		docs, findErr = h.query(ctx, query, args...)
		<-done
	} else {
		docs, findErr = h.query(ctx, query, args...)
	}
	if countErr != nil {
		return ViewListResult{}, countErr
	}
	if findErr != nil {
		return ViewListResult{}, findErr
	}
	for _, doc := range docs {
		normalizeStationHANAView(doc, now)
	}
	return ViewListResult{Items: docs, Page: page, PageSize: pageSize, Total: total}, nil
}

func (s *Store) stationViewListAll(ctx context.Context, f ViewFilters) (ViewListResult, error) {
	h, err := s.stationHANAReader()
	if err != nil {
		return ViewListResult{}, err
	}
	where, args := stationWhere(f)
	total, err := h.count(ctx, where, args...)
	if err != nil {
		return ViewListResult{}, err
	}
	query := stationSelectQuery(where, "", MaxViewQueryRows, 0)
	docs, err := h.query(ctx, query, args...)
	if err != nil {
		return ViewListResult{}, err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	result := make([]bson.M, 0, len(docs))
	for _, doc := range docs {
		normalizeStationHANAView(doc, now)
		projected := make(bson.M, 14)
		for key := range viewAllProjections[stationHANAViewID] {
			if key == "_id" || key == "_source_key" {
				continue
			}
			projected[key] = doc[key]
		}
		projected["_id"] = doc["_id"]
		projected["_source_key"] = doc["_source_key"]
		projected["LINE_CODE"] = nil
		result = append(result, projected)
	}
	return ViewListResult{Items: result, Page: 1, PageSize: len(result), Total: total}, nil
}

func (s *Store) stationViewDetail(ctx context.Context, id string) (ViewDetailResult, error) {
	h, err := s.stationHANAReader()
	if err != nil {
		return ViewDetailResult{}, err
	}
	where, args, ok := stationSourceKeyWhere(id)
	if !ok {
		return ViewDetailResult{}, mongo.ErrNoDocuments
	}
	docs, err := h.query(ctx, stationSelectQuery(where, "", 1, 0), args...)
	if err != nil {
		return ViewDetailResult{}, err
	}
	if len(docs) == 0 {
		return ViewDetailResult{}, mongo.ErrNoDocuments
	}
	doc := docs[0]
	normalizeStationHANAView(doc, time.Now().UTC().Format(time.RFC3339Nano))
	return ViewDetailResult{Item: doc, Fields: viewDetailFields(stationHANAViewID, doc)}, nil
}

func (s *Store) stationViewStats(ctx context.Context, f ViewFilters) (ViewStatsResult, error) {
	h, err := s.stationHANAReader()
	if err != nil {
		return ViewStatsResult{}, err
	}
	where, args := stationWhere(f)
	query := `SELECT COUNT(*), MIN("GSTRS"), MAX("GSTRS"), ` +
		`COALESCE(SUM(CASE WHEN TRIM(COALESCE("KDAUF", '')) = '' THEN 1 ELSE 0 END), 0), ` +
		`COALESCE(SUM(CASE WHEN TRIM(COALESCE("AUFNR", '')) = '' THEN 1 ELSE 0 END), 0) ` +
		`FROM ` + stationHANAView
	if where != "" {
		query += " " + where
	}
	var total, missingSales, missingProduction int64
	var start, end sql.NullString
	if err := h.db.QueryRowContext(ctx, query, args...).Scan(&total, &start, &end, &missingSales, &missingProduction); err != nil {
		return ViewStatsResult{}, err
	}
	return ViewStatsResult{
		Total:                  total,
		MissingSalesOrder:      missingSales,
		MissingProductionOrder: missingProduction,
		DataStartDate:          start.String,
		DataEndDate:            end.String,
		LatestSyncedAt:         time.Now().UTC().Format(time.RFC3339),
	}, nil
}

func (s *Store) stationViewStationStream(ctx context.Context, f ViewFilters, out io.Writer) error {
	useDefaultFilters := f == (ViewFilters{})
	if useDefaultFilters {
		f = defaultStationStreamFilters()
	}
	h, err := s.stationHANAReader()
	if err != nil {
		return err
	}
	where, args := stationWhere(f)
	cacheKey := stationStreamCacheKey(f)
	now := time.Now()
	s.stationCacheMu.RLock()
	cached, ok := s.stationCache[cacheKey]
	s.stationCacheMu.RUnlock()
	if ok {
		if cached.expiresAt.After(now) {
			_, err := out.Write(cached.data)
			return err
		}
		s.stationCacheMu.Lock()
		delete(s.stationCache, cacheKey)
		s.stationCacheMu.Unlock()
	}

	var snapshot bytes.Buffer
	w := bufio.NewWriterSize(io.MultiWriter(out, &snapshot), 1<<20)
	if _, err := io.WriteString(w, strings.Join(stationStreamFields, "\t")+"\n"); err != nil {
		return err
	}
	for offset := 0; offset < MaxStationStreamRows; offset += stationHANAStreamBatch {
		batchLimit := stationHANAStreamBatch
		if remaining := MaxStationStreamRows - offset; remaining < batchLimit {
			batchLimit = remaining
		}
		rowsInBatch := 0
		query := stationStreamQueryPage(where, batchLimit, offset)
		err = h.queryEach(ctx, query, args, func(doc map[string]any) error {
			rowsInBatch++
			row := make([]string, 0, len(stationStreamFields))
			row = append(row, csvValue(stationSourceKeyFromMap(doc)))
			for _, field := range stationStreamFields[1:] {
				value, ok := doc[field]
				if !ok || field == "LINE_CODE" {
					value = ""
				}
				row = append(row, csvValue(value))
			}
			for i := range row {
				row[i] = tsvSanitizer.Replace(row[i])
			}
			_, err := io.WriteString(w, strings.Join(row, "\t")+"\n")
			return err
		})
		if err != nil {
			return err
		}
		if rowsInBatch < batchLimit {
			break
		}
		if offset+batchLimit == MaxStationStreamRows {
			probe, probeErr := h.query(ctx, stationStreamQueryPage(where, 1, MaxStationStreamRows), args...)
			if probeErr != nil {
				return probeErr
			}
			if len(probe) > 0 {
				return fmt.Errorf("%w: 查询结果超过单次上限 %d 条，请缩小筛选范围", ErrStreamQueryTooLarge, MaxStationStreamRows)
			}
			break
		}
	}
	if err := w.Flush(); err != nil {
		return err
	}
	if snapshot.Len() <= stationStreamCacheMaxBytes {
		s.stationCacheMu.Lock()
		s.stationCache[cacheKey] = bomStreamCacheEntry{data: snapshot.Bytes(), expiresAt: time.Now().Add(stationStreamCacheTTL)}
		s.stationCacheMu.Unlock()
	}
	return nil
}

func (s *Store) stationPackagingCounts(ctx context.Context, input string) ([]PackagingCount, error) {
	h, err := s.stationHANAReader()
	if err != nil {
		return nil, err
	}
	items := make([]any, 0)
	for _, value := range strings.Split(input, ",") {
		items = append(items, value)
	}
	values := normalizedOrderCandidates(items)
	if len(values) == 0 {
		return nil, fmt.Errorf("production orders are required")
	}
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(values)), ",")
	query := fmt.Sprintf(`SELECT "AUFNR","PCODE","SPEC","OPERATION" FROM %s WHERE "AUFNR" IN (%s)`, stationHANAView, placeholders)
	args := make([]any, 0, len(values))
	for _, value := range values {
		args = append(args, value)
	}

	type aggregate struct {
		barcodes map[string]struct{}
		missing  bool
	}
	aggregates := make(map[string]*aggregate)
	err = h.queryEach(ctx, query, args, func(doc map[string]any) error {
		station := stationCodeFromValues("", stringValue(doc["SPEC"]), stringValue(doc["OPERATION"]))
		if station != "OP300" && station != "OP310" {
			return nil
		}
		order := normalizeOrderNumber(stringValue(doc["AUFNR"]))
		if order == "" {
			order = strings.TrimSpace(stringValue(doc["AUFNR"]))
		}
		item := aggregates[order]
		if item == nil {
			item = &aggregate{barcodes: make(map[string]struct{})}
			aggregates[order] = item
		}
		barcode := strings.TrimSpace(stringValue(doc["PCODE"]))
		if barcode == "" {
			item.missing = true
			return nil
		}
		item.barcodes[barcode] = struct{}{}
		return nil
	})
	if err != nil {
		return nil, err
	}
	result := make([]PackagingCount, 0, len(aggregates))
	for order, item := range aggregates {
		result = append(result, PackagingCount{ProductionOrder: order, Quantity: int64(len(item.barcodes)), MissingBarcode: item.missing})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].ProductionOrder < result[j].ProductionOrder })
	return result, nil
}

func stationSelectQuery(where, order string, limit, offset int) string {
	query := `SELECT ` + stationHANASelectList + ` FROM ` + stationHANAView
	if where != "" {
		query += " " + where
	}
	if order != "" {
		query += " ORDER BY " + order
	}
	if limit > 0 {
		query += fmt.Sprintf(" LIMIT %d", limit)
	}
	if offset > 0 {
		query += fmt.Sprintf(" OFFSET %d", offset)
	}
	return query
}

func stationStreamQuery(where string, limit int) string {
	return stationStreamQueryPage(where, limit, 0)
}

func stationStreamQueryPage(where string, limit, offset int) string {
	columns := `"HISTROYID","PCODE","AUFNR","KDAUF","GSTRS","SPEC","SPEC_DESC","OPERATION","ACTUAL_START_TIME","ACTUAL_END_TIME","MAKTX_TH","LGORT","SPEC_TIME","OCODE"`
	query := `SELECT ` + columns + ` FROM ` + stationHANAView
	if where != "" {
		query += " " + where
	}
	query += " ORDER BY " + stationHANAOrder + fmt.Sprintf(" LIMIT %d", limit)
	if offset > 0 {
		query += fmt.Sprintf(" OFFSET %d", offset)
	}
	return query
}

func stationWhere(f ViewFilters) (string, []any) {
	conditions := make([]string, 0, 10)
	args := make([]any, 0, 20)

	from, to := f.DateFrom, f.DateTo
	if from == "" {
		from = f.From
	}
	if to == "" {
		to = f.To
	}
	if from = compactStationDate(from); from != "" {
		conditions = append(conditions, `"GSTRS" >= ?`)
		args = append(args, from)
	}
	if to = compactStationDate(to); to != "" {
		conditions = append(conditions, `"GSTRS" <= ?`)
		args = append(args, to)
	}
	if strings.TrimSpace(f.ProductModel) != "" {
		conditions = append(conditions, `"MAKTX_TH" = ?`)
		args = append(args, f.ProductModel)
	}
	if strings.TrimSpace(f.Keyword) != "" {
		pattern := stationLikePattern(f.Keyword)
		parts := make([]string, 0, len(stationHANAKeywordFields))
		for _, field := range stationHANAKeywordFields {
			parts = append(parts, fmt.Sprintf(`LOWER(COALESCE("%s", '')) LIKE ? ESCAPE '\'`, field))
			args = append(args, pattern)
		}
		conditions = append(conditions, "("+strings.Join(parts, " OR ")+")")
	}
	if strings.TrimSpace(f.SN) != "" {
		clause, values := stationExactBatch("PCODE", f.SN, false)
		if clause != "" {
			conditions = append(conditions, clause)
			args = append(args, values...)
		}
	}
	if strings.TrimSpace(f.ProductionOrder) != "" {
		clause, values := stationExactBatch("AUFNR", f.ProductionOrder, true)
		if clause != "" {
			conditions = append(conditions, clause)
			args = append(args, values...)
		}
	}
	if strings.TrimSpace(f.SalesOrder) != "" {
		clause, values := stationExactBatch("KDAUF", f.SalesOrder, true)
		if clause != "" {
			conditions = append(conditions, clause)
			args = append(args, values...)
		}
	}
	if strings.TrimSpace(f.StationCode) != "" {
		conditions = append(conditions, `("PCODE" = ? OR "SPEC" = ? OR "OPERATION" = ?)`)
		args = append(args, f.StationCode, f.StationCode, f.StationCode)
	}
	if strings.TrimSpace(f.Base) != "" {
		conditions = append(conditions, `"LGORT" = ?`)
		args = append(args, f.Base)
	}
	if len(conditions) == 0 {
		return "", nil
	}
	return "WHERE " + strings.Join(conditions, " AND "), args
}

func stationExactBatch(field, input string, leadingZeroCompatible bool) (string, []any) {
	values := splitValues(input)
	if len(values) == 0 {
		return "", nil
	}
	clauses := make([]string, 0, len(values))
	args := make([]any, 0, len(values)*2)
	for _, value := range values {
		if leadingZeroCompatible && isNumericOrder(value) {
			trimmed := normalizeOrderNumber(value)
			clauses = append(clauses, fmt.Sprintf(`(UPPER("%s") = UPPER(?) OR UPPER(COALESCE(NULLIF(LTRIM("%s", '0'), ''), '0')) = UPPER(?))`, field, field))
			args = append(args, value, trimmed)
			continue
		}
		clauses = append(clauses, fmt.Sprintf(`UPPER("%s") = UPPER(?)`, field))
		args = append(args, value)
	}
	if len(clauses) == 1 {
		return clauses[0], args
	}
	return "(" + strings.Join(clauses, " OR ") + ")", args
}

func stationLikePattern(value string) string {
	replacer := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	return "%" + strings.ToLower(replacer.Replace(strings.TrimSpace(value))) + "%"
}

func compactStationDate(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if len(value) >= 10 && value[4] == '-' {
		value = value[:10]
	}
	return strings.ReplaceAll(value, "-", "")
}

func stationSourceKeyWhere(id string) (string, []any, bool) {
	var values []any
	if err := json.Unmarshal([]byte(id), &values); err != nil || len(values) != len(stationHANAKeyFields) {
		return "", nil, false
	}
	clauses := make([]string, 0, len(values))
	args := make([]any, 0, len(values))
	for index, field := range stationHANAKeyFields {
		value, ok := values[index].(string)
		if !ok {
			return "", nil, false
		}
		clauses = append(clauses, fmt.Sprintf(`TRIM(COALESCE("%s", '')) = ?`, field))
		args = append(args, value)
	}
	return "WHERE " + strings.Join(clauses, " AND "), args, true
}

func normalizeStationHANAView(doc bson.M, syncedAt string) {
	key := stationSourceKey(doc)
	doc["_source_key"] = key
	doc["_id"] = stationDocumentID(key)
	doc["_source_view"] = stationHANAViewID
	doc["_sync_run_id"] = ""
	doc["_synced_at"] = syncedAt
	normalizeViewID(doc)
}

func stationSourceKey(doc bson.M) string {
	values := make([]any, len(stationHANAKeyFields))
	for index, field := range stationHANAKeyFields {
		values[index] = stationKeyValue(doc[field])
	}
	return stationJSON(values)
}

func stationSourceKeyFromMap(doc map[string]any) string {
	values := make(map[string]any, len(stationHANAKeyFields))
	for _, field := range stationHANAKeyFields {
		values[field] = doc[field]
	}
	return stationSourceKey(bson.M(values))
}

func stationKeyValue(value any) string {
	if value == nil {
		return ""
	}
	var text string
	switch typed := value.(type) {
	case string:
		text = typed
	case []byte:
		text = string(typed)
	case time.Time:
		text = typed.Format("2006-01-02T15:04:05")
	default:
		text = fmt.Sprint(value)
	}
	return strings.TrimSpace(text)
}

func stationJSON(value any) string {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
	return strings.TrimSuffix(buffer.String(), "\n")
}

func stationDocumentID(key string) string {
	return sha256Hex(key)
}

func sha256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func stationCodeFromValues(lineCode, spec, operation string) string {
	value := strings.TrimSpace(lineCode)
	if value == "" {
		value = strings.TrimSpace(spec)
	}
	if value == "" {
		value = strings.TrimSpace(operation)
	}
	value = strings.ReplaceAll(value, "：", ":")
	if index := strings.Index(value, ":"); index >= 0 {
		value = value[:index]
	}
	if index := strings.LastIndex(value, "-"); index >= 0 {
		value = value[index+1:]
	}
	return strings.TrimSpace(value)
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func (h *stationHANA) count(ctx context.Context, where string, args ...any) (int64, error) {
	query := `SELECT COUNT(*) FROM ` + stationHANAView
	if where != "" {
		query += " " + where
	}
	return h.queryInt64(ctx, query, args...)
}

func (h *stationHANA) queryInt64(ctx context.Context, query string, args ...any) (int64, error) {
	var total int64
	for attempt := 0; attempt < stationHANAQueryAttempts; attempt++ {
		err := h.db.QueryRowContext(ctx, query, args...).Scan(&total)
		if err == nil {
			return total, nil
		}
		if !retryStationHANAQuery(ctx, err, attempt) {
			return 0, err
		}
	}
	return 0, fmt.Errorf("HANA 查询重试失败")
}

func (h *stationHANA) query(ctx context.Context, query string, args ...any) ([]bson.M, error) {
	docs := make([]bson.M, 0)
	err := h.queryEach(ctx, query, args, func(doc map[string]any) error {
		item := make(bson.M, len(doc))
		for key, value := range doc {
			item[key] = value
		}
		docs = append(docs, item)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return docs, nil
}

func (h *stationHANA) queryEach(ctx context.Context, query string, args []any, visit func(map[string]any) error) error {
	for attempt := 0; attempt < stationHANAQueryAttempts; attempt++ {
		visited := false
		err := h.queryEachOnce(ctx, query, args, func(doc map[string]any) error {
			visited = true
			return visit(doc)
		})
		if err == nil {
			return nil
		}
		// Once a row has reached the caller, retrying would duplicate stream
		// output. A bad connection before the first row is safe to replay.
		if visited || !retryStationHANAQuery(ctx, err, attempt) {
			return err
		}
	}
	return fmt.Errorf("HANA 查询重试失败")
}

func (h *stationHANA) queryEachOnce(ctx context.Context, query string, args []any, visit func(map[string]any) error) error {
	rows, err := h.db.QueryContext(ctx, query, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return err
	}
	for rows.Next() {
		values := make([]sql.NullString, len(columns))
		destinations := make([]any, len(columns))
		for index := range values {
			destinations[index] = &values[index]
		}
		if err := rows.Scan(destinations...); err != nil {
			return err
		}
		doc := make(map[string]any, len(columns))
		for index, column := range columns {
			if values[index].Valid {
				doc[column] = values[index].String
			} else {
				doc[column] = nil
			}
		}
		if err := visit(doc); err != nil {
			return err
		}
	}
	return rows.Err()
}

func retryStationHANAQuery(ctx context.Context, err error, attempt int) bool {
	if attempt+1 >= stationHANAQueryAttempts || ctx.Err() != nil || !errors.Is(err, driver.ErrBadConn) {
		return false
	}
	timer := time.NewTimer(time.Duration(attempt+1) * 100 * time.Millisecond)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
