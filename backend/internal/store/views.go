package store

import (
	"context"
	"fmt"
	"sync"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

func (s *Store) ViewList(ctx context.Context, viewID string, f ViewFilters, page, pageSize int) (ViewListResult, error) {
	config, ok := documentedViews[viewID]
	if !ok {
		return ViewListResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	filter := viewFilter(viewID, f, config.searchFields, config.dateField)
	sortFields := bson.D{}
	for _, field := range config.orderFields {
		sortFields = append(sortFields, bson.E{Key: field, Value: -1})
	}

	// Count and page retrieval are independent MongoDB operations. Running
	// them together removes one full network/database round-trip from the
	// request latency, which matters for the unfiltered large station view.
	var total int64
	docs := make([]bson.M, 0)
	var countErr, findErr error
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		total, countErr = s.views[viewID].CountDocuments(ctx, filter)
	}()
	go func() {
		defer wg.Done()
		findOptions := options.Find().SetSort(sortFields).SetSkip(int64((page - 1) * pageSize)).SetLimit(int64(pageSize))
		cur, err := s.views[viewID].Find(ctx, filter, findOptions)
		if err != nil {
			findErr = err
			return
		}
		defer cur.Close(ctx)
		findErr = cur.All(ctx, &docs)
	}()
	wg.Wait()
	if countErr != nil {
		return ViewListResult{}, countErr
	}
	if findErr != nil {
		return ViewListResult{}, findErr
	}
	for _, doc := range docs {
		normalizeViewID(doc)
	}
	return ViewListResult{Items: docs, Page: page, PageSize: pageSize, Total: total}, nil
}

// ViewListAll is intended for server-side calculations such as LRR. It uses
// a narrow projection and a hard row bound instead of forcing thousands of
// small HTTP pages across the gateway boundary.
func (s *Store) ViewListAll(ctx context.Context, viewID string, f ViewFilters) (ViewListResult, error) {
	config, ok := documentedViews[viewID]
	if !ok {
		return ViewListResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	filter := viewFilter(viewID, f, config.searchFields, config.dateField)
	total, err := s.views[viewID].CountDocuments(ctx, filter)
	if err != nil {
		return ViewListResult{}, err
	}
	projection := viewAllProjections[viewID]
	findOptions := options.Find().SetLimit(MaxViewQueryRows).SetBatchSize(10000)
	if projection != nil {
		findOptions.SetProjection(projection)
	}
	cur, err := s.views[viewID].Find(ctx, filter, findOptions)
	if err != nil {
		return ViewListResult{}, err
	}
	defer cur.Close(ctx)
	docs := make([]bson.M, 0)
	if err := cur.All(ctx, &docs); err != nil {
		return ViewListResult{}, err
	}
	// Total intentionally remains the count before the hard query limit so API
	// clients can detect truncation without fetching an unbounded result set.
	return ViewListResult{Items: docs, Page: 1, PageSize: len(docs), Total: total}, nil
}

// ViewBOMStream writes source BOM fields as CSV without JSON map reflection.
// It deliberately exposes raw rows only; LRR grouping and rates stay in the
// quality-monitoring service.
func (s *Store) ViewDetail(ctx context.Context, viewID, id string) (ViewDetailResult, error) {
	if _, ok := documentedViews[viewID]; !ok {
		return ViewDetailResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	var doc bson.M
	err := s.views[viewID].FindOne(ctx, bson.M{"$or": bson.A{bson.M{"_source_key": id}, bson.M{"_id": id}}}).Decode(&doc)
	if err != nil {
		return ViewDetailResult{}, err
	}
	normalizeViewID(doc)
	return ViewDetailResult{Item: doc, Fields: viewDetailFields(viewID, doc)}, nil
}

func (s *Store) ViewStats(ctx context.Context, viewID string, f ViewFilters) (ViewStatsResult, error) {
	config, ok := documentedViews[viewID]
	if !ok {
		return ViewStatsResult{}, fmt.Errorf("unknown view: %s", viewID)
	}
	filter := viewFilter(viewID, f, config.searchFields, config.dateField)
	total, err := s.views[viewID].CountDocuments(ctx, filter)
	if err != nil {
		return ViewStatsResult{}, err
	}
	result := ViewStatsResult{Total: total}
	if config.dateField == "" {
		return result, nil
	}
	cur, err := s.views[viewID].Aggregate(ctx, viewStatsPipeline(viewID, filter, config.dateField))
	if err != nil {
		return ViewStatsResult{}, err
	}
	defer cur.Close(ctx)
	var rows []bson.M
	if err := cur.All(ctx, &rows); err != nil {
		return ViewStatsResult{}, err
	}
	if len(rows) > 0 {
		result.DataStartDate, result.DataEndDate = firstText(rows[0], "dataStartDate"), firstText(rows[0], "dataEndDate")
		result.SalesOrders = int64(number(rows[0]["salesOrders"]))
		result.ProductionOrders = int64(number(rows[0]["productionOrders"]))
		result.MissingSalesOrder = int64(number(rows[0]["missingSalesOrder"]))
		result.MissingProductionOrder = int64(number(rows[0]["missingProductionOrder"]))
		result.MissingProductionOrderDistinct = int64(number(rows[0]["missingProductionOrderDistinct"]))
		if value, ok := rows[0]["latestSyncedAt"]; ok && value != nil {
			result.LatestSyncedAt = fmt.Sprint(value)
		}
	}
	return result, nil
}

func viewStatsPipeline(viewID string, filter bson.M, dateField string) mongo.Pipeline {
	group := bson.M{
		"_id":            nil,
		"dataStartDate":  bson.M{"$min": "$" + dateField},
		"dataEndDate":    bson.M{"$max": "$" + dateField},
		"latestSyncedAt": bson.M{"$max": "$_synced_at"},
	}
	project := bson.M{
		"_id":           0,
		"dataStartDate": 1,
		"dataEndDate":   1,
		"latestSyncedAt": bson.M{"$dateToString": bson.M{
			"date": "$latestSyncedAt", "format": "%Y-%m-%dT%H:%M:%SZ", "timezone": "UTC",
		}},
	}
	if viewID == "Z_V_ZMES_T_001" {
		group["missingSalesOrder"] = bson.M{"$sum": bson.M{"$cond": bson.A{emptyViewField("KDAUF"), 1, 0}}}
		group["missingProductionOrder"] = bson.M{"$sum": bson.M{"$cond": bson.A{emptyViewField("AUFNR"), 1, 0}}}
		project["missingSalesOrder"] = 1
		project["missingProductionOrder"] = 1
	} else if viewID == "ZSGV_ZSD124" {
		group["productionOrderValues"] = bson.M{"$addToSet": normalizedViewField("AUFNR_1")}
		group["salesOrderValues"] = bson.M{"$addToSet": normalizedViewField("VBELN_EX")}
		group["missingProductionOrder"] = bson.M{"$sum": bson.M{"$cond": bson.A{emptyViewField("AUFNR_1"), 1, 0}}}
		group["missingSalesOrder"] = bson.M{"$sum": bson.M{"$cond": bson.A{emptyViewField("VBELN_EX"), 1, 0}}}
		group["missingProductionOrderValues"] = bson.M{"$addToSet": bson.M{"$cond": bson.A{
			emptyViewField("AUFNR_1"),
			bson.M{"$cond": bson.A{emptyViewField("VBELN_EX"), normalizedViewField("_source_key"), normalizedViewField("VBELN_EX")}},
			"",
		}}}
		project["productionOrders"] = bson.M{"$size": bson.M{"$setDifference": bson.A{"$productionOrderValues", bson.A{""}}}}
		project["salesOrders"] = bson.M{"$size": bson.M{"$setDifference": bson.A{"$salesOrderValues", bson.A{""}}}}
		project["missingProductionOrder"] = 1
		project["missingSalesOrder"] = 1
		project["missingProductionOrderDistinct"] = bson.M{"$size": bson.M{"$setDifference": bson.A{"$missingProductionOrderValues", bson.A{"", nil}}}}
	}
	return mongo.Pipeline{
		{{Key: "$match", Value: filter}},
		{{Key: "$group", Value: group}},
		{{Key: "$project", Value: project}},
	}
}

func emptyViewField(field string) bson.M {
	return bson.M{"$eq": bson.A{normalizedViewField(field), ""}}
}

func normalizedViewField(field string) bson.M {
	return bson.M{"$trim": bson.M{"input": bson.M{"$convert": bson.M{
		"input": "$" + field, "to": "string", "onError": "", "onNull": "",
	}}}}
}
