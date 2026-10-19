package store

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func (s *Store) OrderModels(ctx context.Context, keyword string) (OrderModelsResult, error) {
	filter := bson.M{"data.MAKTX_TH": bson.M{"$exists": true, "$nin": bson.A{"", nil}}}
	if keyword = strings.TrimSpace(keyword); keyword != "" {
		filter["data.MAKTX_TH"] = bson.M{"$regex": regexp.QuoteMeta(keyword), "$options": "i"}
	}
	values, err := s.orders.Distinct(ctx, "data.MAKTX_TH", filter)
	if err != nil {
		return OrderModelsResult{}, err
	}
	items := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		text := strings.TrimSpace(fmt.Sprint(value))
		if text == "" || text == "<nil>" {
			continue
		}
		if _, ok := seen[text]; ok {
			continue
		}
		seen[text] = struct{}{}
		items = append(items, text)
	}
	sort.Strings(items)
	return OrderModelsResult{Items: items}, nil
}

func (s *Store) OrderCustomerIDs(ctx context.Context) (OrderCustomerIDsResult, error) {
	filter := bson.M{"data.KID": bson.M{"$exists": true, "$nin": bson.A{"", nil}}}
	values, err := s.orders.Distinct(ctx, "data.KID", filter)
	if err != nil {
		return OrderCustomerIDsResult{}, err
	}
	items := make([]string, 0, len(values))
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		text := strings.TrimSpace(fmt.Sprint(value))
		if text == "" || text == "<nil>" {
			continue
		}
		if _, ok := seen[text]; ok {
			continue
		}
		seen[text] = struct{}{}
		items = append(items, text)
	}
	sort.Strings(items)
	return OrderCustomerIDsResult{Items: items}, nil
}

func (s *Store) OrderOptions(ctx context.Context, base string) (OrderOptionsResult, error) {
	base = strings.TrimSpace(base)
	s.orderOptionsCacheMu.Lock()
	if s.orderOptionsCache != nil {
		if cached, ok := s.orderOptionsCache[base]; ok && time.Since(s.orderOptionsCacheAt) < orderOptionsCacheTTL {
			s.orderOptionsCacheMu.Unlock()
			return cloneOrderOptions(cached), nil
		}
	}
	s.orderOptionsCacheMu.Unlock()

	fields := map[string]string{
		"productionOrders": "aufnr",
		"salesOrders":      "data.VBELN",
		"materialNumbers":  "data.MATNR",
		"productModels":    "data.MAKTX_TH",
		"bases":            "data.LGORT",
		"customerIds":      "data.KID",
	}
	values := make(map[string][]string, len(fields))
	for name, field := range fields {
		items, err := s.orders.Distinct(ctx, field, orderOptionFilter(base, field))
		if err != nil {
			return OrderOptionsResult{}, err
		}
		normalized := make([]string, 0, len(items))
		seen := make(map[string]struct{}, len(items))
		for _, item := range items {
			value := strings.TrimSpace(fmt.Sprint(item))
			if value == "" || value == "<nil>" {
				continue
			}
			if _, ok := seen[value]; ok {
				continue
			}
			seen[value] = struct{}{}
			normalized = append(normalized, value)
		}
		if len(normalized) > MaxOrderOptionValues {
			return OrderOptionsResult{}, fmt.Errorf("订单选项 %s 超过 %d 条，请增加基地筛选", name, MaxOrderOptionValues)
		}
		sort.Strings(normalized)
		values[name] = normalized
	}
	result := OrderOptionsResult{
		ProductionOrders: values["productionOrders"],
		SalesOrders:      values["salesOrders"],
		MaterialNumbers:  values["materialNumbers"],
		ProductModels:    values["productModels"],
		Bases:            values["bases"],
		CustomerIDs:      values["customerIds"],
	}
	s.orderOptionsCacheMu.Lock()
	if s.orderOptionsCache == nil {
		s.orderOptionsCache = make(map[string]OrderOptionsResult)
	}
	s.orderOptionsCache[base] = cloneOrderOptions(result)
	s.orderOptionsCacheAt = time.Now()
	s.orderOptionsCacheMu.Unlock()
	return result, nil
}

func orderOptionFilter(base, field string) bson.M {
	filter := bson.M{
		field: bson.M{"$exists": true, "$nin": bson.A{"", nil}},
	}
	if base != "" {
		filter["data.LGORT"] = base
	}
	return filter
}

func cloneOrderOptions(value OrderOptionsResult) OrderOptionsResult {
	return OrderOptionsResult{
		ProductionOrders: append([]string(nil), value.ProductionOrders...),
		SalesOrders:      append([]string(nil), value.SalesOrders...),
		MaterialNumbers:  append([]string(nil), value.MaterialNumbers...),
		ProductModels:    append([]string(nil), value.ProductModels...),
		Bases:            append([]string(nil), value.Bases...),
		CustomerIDs:      append([]string(nil), value.CustomerIDs...),
	}
}

func (s *Store) Orders(ctx context.Context, f OrderFilters, page, pageSize int) (OrderListResult, error) {
	filter := orderFilter(f)
	total, err := s.orders.CountDocuments(ctx, filter)
	if err != nil {
		return OrderListResult{}, err
	}
	// Synced orders often share both timestamps; _id keeps page boundaries stable.
	cur, err := s.orders.Find(ctx, filter, options.Find().SetProjection(orderListProjection).SetSort(bson.D{{Key: "data.GSTRS", Value: -1}, {Key: "last_synced_at", Value: -1}, {Key: "_id", Value: -1}}).SetSkip(int64((page-1)*pageSize)).SetLimit(int64(pageSize)).SetBatchSize(10000))
	if err != nil {
		return OrderListResult{}, err
	}
	defer cur.Close(ctx)
	docs := make([]bson.M, 0)
	if err = cur.All(ctx, &docs); err != nil {
		return OrderListResult{}, err
	}
	items := make([]Order, 0, len(docs))
	for _, doc := range docs {
		items = append(items, normalizeOrder(doc))
	}
	return OrderListResult{Items: items, Page: page, PageSize: pageSize, Total: total}, nil
}

// OrdersAll returns every filtered order up to the API's bounded bulk-query limit.
// It intentionally reuses the same filter and ordering as the paginated board.
func (s *Store) OrdersAll(ctx context.Context, f OrderFilters) (OrderListResult, error) {
	return s.Orders(ctx, f, 1, MaxOrderQueryRows)
}

func (s *Store) OrderDetail(ctx context.Context, id string) (OrderDetail, error) {
	var doc bson.M
	if err := s.orders.FindOne(ctx, bson.M{"_id": id}).Decode(&doc); err != nil {
		return OrderDetail{}, err
	}
	return OrderDetail{Order: normalizeOrder(doc), Fields: orderDetailFields(doc), Raw: doc}, nil
}
