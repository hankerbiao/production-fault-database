package store

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/bson/primitive"
	"go.mongodb.org/mongo-driver/mongo/options"
)

var ErrStreamQueryTooLarge = errors.New("stream query exceeds the configured row limit")
var ErrSCSStreamScopeRequired = errors.New("SCS stream query requires at least one filter")

func (s *Store) ViewBOMStream(ctx context.Context, f ViewFilters, out io.Writer) error {
	if err := ValidateBOMFilter(f); err != nil {
		return err
	}
	return s.viewBOMStream(ctx, f, out)
}

func ValidateBOMFilter(f ViewFilters) error {
	if hasBOMFilter(f) {
		return nil
	}
	return fmt.Errorf("BOM 查询必须包含日期、物料号或订单筛选条件，未限定范围的查询已禁用")
}

func (s *Store) viewBOMStream(ctx context.Context, f ViewFilters, out io.Writer) error {
	config, ok := documentedViews["ZSGV_ZSD124"]
	if !ok {
		return fmt.Errorf("unknown view: ZSGV_ZSD124")
	}
	filter := viewFilter("ZSGV_ZSD124", f, config.searchFields, config.dateField)
	cacheKey := bomStreamCacheKey(f)
	now := time.Now()
	s.bomCacheMu.RLock()
	cached, ok := s.bomCache[cacheKey]
	s.bomCacheMu.RUnlock()
	if ok {
		if cached.expiresAt.After(now) {
			_, err := out.Write(cached.data)
			return err
		}
		s.bomCacheMu.Lock()
		delete(s.bomCache, cacheKey)
		s.bomCacheMu.Unlock()
	}
	projection := bson.M{"_id": 0, "_source_key": 1}
	for _, field := range bomStreamFields[1:] {
		projection[field] = 1
	}
	cur, err := s.views["ZSGV_ZSD124"].Find(ctx, filter, options.Find().SetProjection(projection).SetLimit(MaxViewQueryRows).SetBatchSize(10000))
	if err != nil {
		return err
	}
	defer cur.Close(ctx)
	// Tee the raw stream into a bounded in-memory snapshot. The first request
	// still streams rows to the caller; subsequent identical reads avoid a
	// repeated Mongo scan while the source remains unchanged for this short TTL.
	var snapshot bytes.Buffer
	w := bufio.NewWriterSize(io.MultiWriter(out, &snapshot), 1<<20)
	if _, err := io.WriteString(w, strings.Join(bomStreamFields, "\t")+"\n"); err != nil {
		return err
	}
	for cur.Next(ctx) {
		var doc bson.M
		if err := cur.Decode(&doc); err != nil {
			return err
		}
		row := make([]string, 0, len(bomStreamFields))
		for _, field := range bomStreamFields {
			value := doc[field]
			if field == "id" {
				value = doc["_source_key"]
			}
			row = append(row, tsvSanitizer.Replace(csvValue(value)))
		}
		if _, err := io.WriteString(w, strings.Join(row, "\t")+"\n"); err != nil {
			return err
		}
	}
	if err := cur.Err(); err != nil {
		return err
	}
	if err := w.Flush(); err != nil {
		return err
	}
	if snapshot.Len() <= bomStreamCacheMaxBytes {
		s.bomCacheMu.Lock()
		s.bomCache[cacheKey] = bomStreamCacheEntry{data: snapshot.Bytes(), expiresAt: time.Now().Add(bomStreamCacheTTL)}
		s.bomCacheMu.Unlock()
	}
	return nil
}

// ViewSCSStream exposes the normalized SCS source fields for server-side
// calculations without the 10,000-row JSON endpoint limit.
func (s *Store) ViewSCSStream(ctx context.Context, viewID string, f ViewFilters, out io.Writer) error {
	config, ok := documentedViews[viewID]
	if !ok || (viewID != "SCS_DOA" && viewID != "SCS_CHANGE") {
		return fmt.Errorf("unknown view: %s", viewID)
	}
	filter := viewFilter(viewID, f, config.searchFields, config.dateField)
	if len(filter) == 0 {
		return ErrSCSStreamScopeRequired
	}
	if err := ensureStreamRowLimit(ctx, s.views[viewID], filter, MaxSCSStreamRows); err != nil {
		return err
	}
	fields := scsStreamFields(viewID)
	projection := bson.M{"_id": 1}
	for _, field := range fields[1:] {
		if field != "host_model" {
			projection[field] = 1
		}
	}
	if viewID == "SCS_DOA" {
		projection["details.equipment.product_fields.主机型号"] = 1
		projection["details.equipment.host_model"] = 1
	}
	cur, err := s.views[viewID].Find(ctx, filter, options.Find().SetProjection(projection).SetSort(bson.D{{Key: config.orderFields[0], Value: -1}}).SetLimit(MaxSCSStreamRows).SetBatchSize(10000))
	if err != nil {
		return err
	}
	defer cur.Close(ctx)
	w := bufio.NewWriterSize(out, 1<<20)
	if _, err := io.WriteString(w, strings.Join(fields, "\t")+"\n"); err != nil {
		return err
	}
	for cur.Next(ctx) {
		var doc bson.M
		if err := cur.Decode(&doc); err != nil {
			return err
		}
		row := make([]string, 0, len(fields))
		for _, field := range fields {
			value := doc[field]
			switch field {
			case "id":
				value = doc["_id"]
			case "host_model":
				value = scsDOAStreamHostModel(doc)
			}
			row = append(row, tsvSanitizer.Replace(csvValue(value)))
		}
		if _, err := io.WriteString(w, strings.Join(row, "\t")+"\n"); err != nil {
			return err
		}
	}
	if err := cur.Err(); err != nil {
		return err
	}
	return w.Flush()
}

func scsStreamFields(viewID string) []string {
	if viewID == "SCS_DOA" {
		return []string{"id", "doa_code", "doa_type", "service_uid", "status", "doa_judge", "customer_uid", "sugon_sn", "spare_part_sn", "product_name", "problem_batch_number", "declare_time", "acceptance_time", "sales_order", "is_5000_company", "factory_date", "warranty_end_date", "enrichment_status", "GSTRS", "gstrs_status", "production_orders", "shipment_date", "host_model"}
	}
	return []string{"id", "so_code", "customer_name", "device_sn", "change_type", "part_number", "part_sn", "part_name", "operator", "need_return", "is_revoked", "create_time", "is_5000_company"}
}

func scsDOAStreamHostModel(doc bson.M) string {
	equipment, _ := mapDoc(doc["details"])["equipment"].(bson.A)
	var unique string
	for _, item := range equipment {
		device := mapDoc(item)
		model, _ := mapDoc(device["product_fields"])["主机型号"].(string)
		model = strings.TrimSpace(model)
		if model == "" {
			legacy, _ := device["host_model"].(string)
			model = strings.TrimSpace(legacy)
		}
		if model == "" {
			continue
		}
		if unique != "" && unique != model {
			return ""
		}
		unique = model
	}
	return unique
}

func ensureStreamRowLimit(ctx context.Context, collection interface {
	CountDocuments(context.Context, interface{}, ...*options.CountOptions) (int64, error)
}, filter bson.M, limit int) error {
	count, err := collection.CountDocuments(ctx, filter, options.Count().SetLimit(int64(limit+1)))
	if err != nil {
		return err
	}
	if count > int64(limit) {
		return fmt.Errorf("%w: 查询结果约 %d 条，超过单次上限 %d 条，请缩小筛选范围", ErrStreamQueryTooLarge, count, limit)
	}
	return nil
}

func bomStreamCacheKey(f ViewFilters) string {
	return strings.Join([]string{f.Keyword, f.From, f.To, f.DateFrom, f.DateTo, f.StationCode, f.SN, f.ProductionOrder, f.SalesOrder, f.Base, f.ProductModel, f.HeadOrder, f.ItemOrder, f.HeadSN, f.ItemSN, f.MaterialCode, f.CPX, strconv.FormatBool(f.MissingSalesOrder)}, "\x00")
}

func hasBOMFilter(f ViewFilters) bool {
	return strings.TrimSpace(f.Keyword) != "" ||
		strings.TrimSpace(f.From) != "" ||
		strings.TrimSpace(f.To) != "" ||
		strings.TrimSpace(f.DateFrom) != "" ||
		strings.TrimSpace(f.DateTo) != "" ||
		strings.TrimSpace(f.StationCode) != "" ||
		strings.TrimSpace(f.SN) != "" ||
		strings.TrimSpace(f.ProductionOrder) != "" ||
		strings.TrimSpace(f.SalesOrder) != "" ||
		strings.TrimSpace(f.Base) != "" ||
		strings.TrimSpace(f.ProductModel) != "" ||
		strings.TrimSpace(f.HeadOrder) != "" ||
		strings.TrimSpace(f.ItemOrder) != "" ||
		strings.TrimSpace(f.HeadSN) != "" ||
		strings.TrimSpace(f.ItemSN) != "" ||
		strings.TrimSpace(f.MaterialCode) != "" ||
		strings.TrimSpace(f.CPX) != "" ||
		f.MissingSalesOrder
}

func csvValue(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	case []byte:
		return string(typed)
	case primitive.Decimal128:
		return typed.String()
	case primitive.ObjectID:
		return typed.Hex()
	case time.Time:
		return typed.Format(time.RFC3339)
	case int:
		return strconv.Itoa(typed)
	case int32:
		return strconv.FormatInt(int64(typed), 10)
	case int64:
		return strconv.FormatInt(typed, 10)
	case float32:
		return strconv.FormatFloat(float64(typed), 'f', -1, 32)
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(typed)
	default:
		return fmt.Sprint(value)
	}
}
