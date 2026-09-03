package store

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strings"

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
func (s *Store) Orders(ctx context.Context, f OrderFilters, page, pageSize int) (OrderListResult, error) {
	filter := orderFilter(f)
	total, err := s.orders.CountDocuments(ctx, filter)
	if err != nil {
		return OrderListResult{}, err
	}
	cur, err := s.orders.Find(ctx, filter, options.Find().SetProjection(orderListProjection).SetSort(bson.D{{Key: "data.GSTRS", Value: -1}, {Key: "last_synced_at", Value: -1}}).SetSkip(int64((page-1)*pageSize)).SetLimit(int64(pageSize)).SetBatchSize(10000))
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
