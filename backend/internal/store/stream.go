package store

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/bson/primitive"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func (s *Store) ViewBOMStream(ctx context.Context, f ViewFilters, out io.Writer) error {
	if isEmptyBOMFilter(f) && s.bomWarmDone != nil {
		select {
		case <-s.bomWarmDone:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return s.viewBOMStream(ctx, f, out)
}

func (s *Store) prewarmBOMStream() {
	defer close(s.bomWarmDone)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()
	_ = s.viewBOMStream(ctx, ViewFilters{}, io.Discard)
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
		row := []string{csvValue(doc["_source_key"]), csvValue(doc["AUFNR_1"]), csvValue(doc["VBELN_EX"]), csvValue(doc["MENGE_A"]), csvValue(doc["MATNR"]), csvValue(doc["LGORT"]), csvValue(doc["BUDAT_MKPF"])}
		for i := range row {
			row[i] = tsvSanitizer.Replace(row[i])
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

// ViewStationStream exposes the narrow station projection used by station RTY.
// It avoids JSON map reflection for the large raw station result set.
func (s *Store) ViewStationStream(ctx context.Context, f ViewFilters, out io.Writer) error {
	if f == defaultStationStreamFilters() && s.stationWarmDone != nil {
		select {
		case <-s.stationWarmDone:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return s.viewStationStream(ctx, f, out)
}

func defaultStationStreamFilters() ViewFilters {
	end := time.Now()
	return ViewFilters{DateFrom: end.AddDate(0, 0, -29).Format("2006-01-02"), DateTo: end.Format("2006-01-02")}
}

func (s *Store) prewarmStationStream() {
	defer close(s.stationWarmDone)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()
	_ = s.viewStationStream(ctx, defaultStationStreamFilters(), io.Discard)
}

func (s *Store) viewStationStream(ctx context.Context, f ViewFilters, out io.Writer) error {
	config, ok := documentedViews["Z_V_ZMES_T_001"]
	if !ok {
		return fmt.Errorf("unknown view: Z_V_ZMES_T_001")
	}
	filter := viewFilter("Z_V_ZMES_T_001", f, config.searchFields, config.dateField)
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
	projection := bson.M{"_id": 0, "_source_key": 1}
	for _, field := range stationStreamFields[1:] {
		projection[field] = 1
	}
	cur, err := s.views["Z_V_ZMES_T_001"].Find(ctx, filter, options.Find().SetProjection(projection).SetLimit(MaxViewQueryRows).SetBatchSize(10000))
	if err != nil {
		return err
	}
	defer cur.Close(ctx)
	var snapshot bytes.Buffer
	w := bufio.NewWriterSize(io.MultiWriter(out, &snapshot), 1<<20)
	if _, err := io.WriteString(w, strings.Join(stationStreamFields, "\t")+"\n"); err != nil {
		return err
	}
	for cur.Next(ctx) {
		var doc bson.M
		if err := cur.Decode(&doc); err != nil {
			return err
		}
		row := make([]string, 0, len(stationStreamFields))
		row = append(row, csvValue(doc["_source_key"]))
		for _, field := range stationStreamFields[1:] {
			row = append(row, csvValue(doc[field]))
		}
		for i := range row {
			row[i] = tsvSanitizer.Replace(row[i])
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
	if snapshot.Len() <= stationStreamCacheMaxBytes {
		s.stationCacheMu.Lock()
		s.stationCache[cacheKey] = bomStreamCacheEntry{data: snapshot.Bytes(), expiresAt: time.Now().Add(stationStreamCacheTTL)}
		s.stationCacheMu.Unlock()
	}
	return nil
}

func bomStreamCacheKey(f ViewFilters) string {
	return strings.Join([]string{f.Keyword, f.From, f.To, f.DateFrom, f.DateTo, f.StationCode, f.SN, f.ProductionOrder, f.SalesOrder, f.Base, f.ProductModel, f.HeadOrder, f.ItemOrder, f.HeadSN, f.ItemSN, f.MaterialCode, strconv.FormatBool(f.MissingSalesOrder)}, "\x00")
}

func stationStreamCacheKey(f ViewFilters) string {
	return strings.Join([]string{f.Keyword, f.From, f.To, f.DateFrom, f.DateTo, f.StationCode, f.SN, f.ProductionOrder, f.SalesOrder, f.Base, f.ProductModel}, "\x00")
}

func isEmptyBOMFilter(f ViewFilters) bool {
	return f == (ViewFilters{})
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
