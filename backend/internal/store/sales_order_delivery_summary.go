package store

import (
	"context"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

// RefreshSalesOrderDeliverySummary atomically publishes materialized
// unfiltered sales-order summaries. Filtered queries keep using the source
// collection so their existing row-level filter semantics remain unchanged.
func (s *Store) RefreshSalesOrderDeliverySummary(ctx context.Context) (refreshErr error) {
	if s.salesOrderDeliverySummaryMeta == nil || s.views["ZSGV_ZSD46N_SO_KK"] == nil {
		return fmt.Errorf("sales order delivery collections are not initialized")
	}

	s.salesOrderDeliverySummaryRefreshMu.Lock()
	defer s.salesOrderDeliverySummaryRefreshMu.Unlock()
	defer func() {
		if refreshErr != nil {
			s.salesOrderDeliverySummaryStateMu.Lock()
			s.salesOrderDeliverySummaryAvailable = false
			s.salesOrderDeliverySummaryStateMu.Unlock()
		}
		s.salesOrderDeliverySummaryReadyOnce.Do(func() {
			if s.salesOrderDeliverySummaryReady != nil {
				close(s.salesOrderDeliverySummaryReady)
			}
		})
	}()

	salesOrderDeliverySummaryPrefix := salesOrderDeliverySummaryCollection + "_build_"
	stagingName := fmt.Sprintf("%s%d", salesOrderDeliverySummaryPrefix, time.Now().UTC().UnixNano())
	staging := s.db.Collection(stagingName)
	activated := false
	defer func() {
		if activated {
			return
		}
		dropCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := staging.Drop(dropCtx); err != nil && err != mongo.ErrNilDocument {
			log.Printf("sales order delivery summary staging cleanup failed: %v", err)
		}
	}()

	sortIndex := bson.D{{Key: "ERDAT", Value: -1}, {Key: "VBELN", Value: -1}, {Key: "_source_key", Value: -1}}
	if _, err := staging.Indexes().CreateOne(ctx, mongo.IndexModel{
		Keys: sortIndex, Options: options.Index().SetName("sales_order_delivery_summary_order"),
	}); err != nil {
		return fmt.Errorf("create sales order delivery summary index: %w", err)
	}

	pipeline := salesOrderDeliverySummaryPipeline(bson.M{})
	pipeline = append(pipeline, bson.D{{Key: "$merge", Value: bson.M{
		"into": stagingName, "on": "_id", "whenMatched": "replace", "whenNotMatched": "insert",
	}}})
	cursor, err := s.views["ZSGV_ZSD46N_SO_KK"].Aggregate(ctx, pipeline, options.Aggregate().SetAllowDiskUse(true).SetBatchSize(1000))
	if err != nil {
		return fmt.Errorf("aggregate sales order delivery summaries: %w", err)
	}
	for cursor.Next(ctx) {
	}
	cursorErr := cursor.Err()
	closeErr := cursor.Close(ctx)
	if cursorErr != nil {
		return fmt.Errorf("write sales order delivery summaries: %w", cursorErr)
	}
	if closeErr != nil {
		return fmt.Errorf("close sales order delivery summary cursor: %w", closeErr)
	}

	_, err = s.salesOrderDeliverySummaryMeta.UpdateOne(ctx, bson.M{"_id": "active"}, bson.M{"$set": bson.M{
		"collection": stagingName, "built_at": time.Now().UTC(),
	}}, options.Update().SetUpsert(true))
	if err != nil {
		return fmt.Errorf("publish sales order delivery summary: %w", err)
	}
	activated = true
	s.salesOrderDeliverySummaryStateMu.Lock()
	s.salesOrderDeliverySummaryAvailable = true
	s.salesOrderDeliverySummaryStateMu.Unlock()
	s.cleanupOldSalesOrderDeliverySummaries(ctx, salesOrderDeliverySummaryPrefix, stagingName)
	return nil
}

func (s *Store) waitForSalesOrderDeliverySummary(ctx context.Context) (bool, error) {
	if s.salesOrderDeliverySummaryReady != nil {
		select {
		case <-s.salesOrderDeliverySummaryReady:
		case <-ctx.Done():
			return false, ctx.Err()
		}
	}
	s.salesOrderDeliverySummaryStateMu.RLock()
	defer s.salesOrderDeliverySummaryStateMu.RUnlock()
	return s.salesOrderDeliverySummaryAvailable, nil
}

func (s *Store) activeSalesOrderDeliverySummary(ctx context.Context) (*mongo.Collection, error) {
	var metadata struct {
		Collection string `bson:"collection"`
	}
	if err := s.salesOrderDeliverySummaryMeta.FindOne(ctx, bson.M{"_id": "active"}).Decode(&metadata); err != nil {
		return nil, err
	}
	prefix := salesOrderDeliverySummaryCollection + "_build_"
	if !strings.HasPrefix(metadata.Collection, prefix) {
		return nil, fmt.Errorf("invalid sales order delivery summary collection %q", metadata.Collection)
	}
	return s.db.Collection(metadata.Collection), nil
}

func (s *Store) cleanupOldSalesOrderDeliverySummaries(ctx context.Context, prefix, activeName string) {
	names, err := s.db.ListCollectionNames(ctx, bson.M{"name": bson.M{"$regex": "^" + prefix}})
	if err != nil {
		log.Printf("list old sales order delivery summaries failed: %v", err)
		return
	}
	cutoff := time.Now().Add(-24 * time.Hour).UnixNano()
	for _, name := range names {
		if name == activeName || !strings.HasPrefix(name, prefix) {
			continue
		}
		createdAt, err := strconv.ParseInt(strings.TrimPrefix(name, prefix), 10, 64)
		if err != nil || createdAt >= cutoff {
			continue
		}
		if err := s.db.Collection(name).Drop(ctx); err != nil {
			log.Printf("drop old sales order delivery summary %s failed: %v", name, err)
		}
	}
}

func (s *Store) viewSalesOrderDeliverySummaryCollection(ctx context.Context, page, pageSize int, preview bool) (ViewListResult, error) {
	collection, err := s.activeSalesOrderDeliverySummary(ctx)
	if err != nil {
		return ViewListResult{}, err
	}
	limit := int64(pageSize)
	if preview {
		limit++
	}
	findOptions := options.Find().
		SetSort(bson.D{{Key: "ERDAT", Value: -1}, {Key: "VBELN", Value: -1}, {Key: "_source_key", Value: -1}}).
		SetSkip(int64((page - 1) * pageSize)).
		SetLimit(limit)
	cursor, err := collection.Find(ctx, bson.M{}, findOptions)
	if err != nil {
		return ViewListResult{}, err
	}
	defer cursor.Close(ctx)
	docs := make([]bson.M, 0, limit)
	if err := cursor.All(ctx, &docs); err != nil {
		return ViewListResult{}, err
	}
	hasMore := false
	if preview && len(docs) > pageSize {
		hasMore = true
		docs = docs[:pageSize]
	}
	for _, doc := range docs {
		delete(doc, "_id")
		hideSalesOrderDeliveryPrivateFields("ZSGV_ZSD46N_SO_KK", doc)
		normalizeViewID(doc)
	}
	if preview {
		return ViewListResult{
			Items: docs, Page: page, PageSize: pageSize, Total: int64(len(docs)),
			HasMore: hasMore, Preview: true,
		}, nil
	}
	total, err := collection.EstimatedDocumentCount(ctx)
	if err != nil {
		return ViewListResult{}, err
	}
	return ViewListResult{Items: docs, Page: page, PageSize: pageSize, Total: total}, nil
}

func (s *Store) viewSalesOrderDeliverySummaryAll(ctx context.Context) (ViewListResult, error) {
	collection, err := s.activeSalesOrderDeliverySummary(ctx)
	if err != nil {
		return ViewListResult{}, err
	}
	total, err := collection.EstimatedDocumentCount(ctx)
	if err != nil {
		return ViewListResult{}, err
	}
	cursor, err := collection.Find(ctx, bson.M{}, options.Find().
		SetSort(bson.D{{Key: "ERDAT", Value: -1}, {Key: "VBELN", Value: -1}, {Key: "_source_key", Value: -1}}).
		SetLimit(MaxSalesOrderDeliverySummaryRows))
	if err != nil {
		return ViewListResult{}, err
	}
	defer cursor.Close(ctx)
	docs := make([]bson.M, 0)
	if err := cursor.All(ctx, &docs); err != nil {
		return ViewListResult{}, err
	}
	for _, doc := range docs {
		delete(doc, "_id")
		hideSalesOrderDeliveryPrivateFields("ZSGV_ZSD46N_SO_KK", doc)
		normalizeViewID(doc)
	}
	return ViewListResult{Items: docs, Page: 1, PageSize: len(docs), Total: total}, nil
}
