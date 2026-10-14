package store

import (
	"context"
	"fmt"
	"log"
	"os"
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
		s.markSalesOrderDeliverySummaryReady()
	}()
	sourceVersion, err := s.salesOrderDeliverySourceVersion(ctx)
	if err != nil {
		return err
	}
	if active, err := s.salesOrderDeliverySyncActive(ctx); err != nil || active {
		if err != nil {
			return err
		}
		return fmt.Errorf("sales order delivery synchronization is active")
	}

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

	currentVersion, err := s.salesOrderDeliverySourceVersion(ctx)
	if err != nil {
		return err
	}
	if currentVersion != sourceVersion {
		return fmt.Errorf("sales order delivery source changed during summary build")
	}
	if active, err := s.salesOrderDeliverySyncActive(ctx); err != nil || active {
		if err != nil {
			return err
		}
		return fmt.Errorf("sales order delivery synchronization started during summary build")
	}
	_, err = s.salesOrderDeliverySummaryMeta.UpdateOne(ctx, bson.M{"_id": "active"}, bson.M{"$set": bson.M{
		"collection": stagingName, "built_at": time.Now().UTC(), "source_version": sourceVersion,
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

func (s *Store) InitializeSalesOrderDeliverySummary(ctx context.Context) error {
	if s.salesOrderDeliverySummaryMeta == nil {
		return fmt.Errorf("sales order delivery summary metadata collection is not initialized")
	}
	defer s.markSalesOrderDeliverySummaryReady()

	s.salesOrderDeliverySummaryRefreshMu.Lock()
	var metadata struct {
		Collection    string `bson:"collection"`
		SourceVersion string `bson:"source_version"`
	}
	err := s.salesOrderDeliverySummaryMeta.FindOne(ctx, bson.M{"_id": "active"}).Decode(&metadata)
	if err == nil {
		prefix := salesOrderDeliverySummaryCollection + "_build_"
		if strings.HasPrefix(metadata.Collection, prefix) {
			names, listErr := s.db.ListCollectionNames(ctx, bson.M{"name": metadata.Collection})
			version, versionErr := s.salesOrderDeliverySourceVersion(ctx)
			if versionErr != nil {
				s.salesOrderDeliverySummaryRefreshMu.Unlock()
				return versionErr
			}
			if listErr == nil && len(names) > 0 && metadata.SourceVersion == version {
				s.salesOrderDeliverySummaryStateMu.Lock()
				s.salesOrderDeliverySummaryAvailable = true
				s.salesOrderDeliverySummaryStateMu.Unlock()
				s.salesOrderDeliverySummaryRefreshMu.Unlock()
				return nil
			}
			if listErr != nil {
				err = listErr
			} else {
				err = mongo.ErrNoDocuments
			}
		} else {
			err = mongo.ErrNoDocuments
		}
	}
	s.salesOrderDeliverySummaryRefreshMu.Unlock()

	if err != nil && err != mongo.ErrNoDocuments {
		return err
	}
	return s.RefreshSalesOrderDeliverySummary(ctx)
}

func (s *Store) salesOrderDeliverySourceVersion(ctx context.Context) (string, error) {
	var version struct {
		RunID string `bson:"run_id"`
	}
	err := s.db.Collection("sync_dataset_versions").FindOne(ctx, bson.M{"_id": "ZSGV_ZSD46N_SO_KK"}).Decode(&version)
	if err == mongo.ErrNoDocuments {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("read delivery source version: %w", err)
	}
	return version.RunID, nil
}

func (s *Store) salesOrderDeliverySyncActive(ctx context.Context) (bool, error) {
	collection := os.Getenv("SYNC_LOCK_COLLECTION")
	if collection == "" {
		collection = "sync_locks"
	}
	err := s.db.Collection(collection).FindOne(ctx, bson.M{
		"_id":        "dataset:" + s.views["ZSGV_ZSD46N_SO_KK"].Name(),
		"expires_at": bson.M{"$gt": time.Now().UTC()},
	}).Err()
	if err == mongo.ErrNoDocuments {
		return false, nil
	}
	return err == nil, err
}

// RefreshSalesOrderDeliverySummaryIfStale is shared by the periodic monitor.
func (s *Store) RefreshSalesOrderDeliverySummaryIfStale(ctx context.Context) error {
	active, err := s.salesOrderDeliverySyncActive(ctx)
	if err != nil {
		return err
	}
	if active {
		s.salesOrderDeliverySummaryStateMu.Lock()
		s.salesOrderDeliverySummaryAvailable = false
		s.salesOrderDeliverySummaryStateMu.Unlock()
		return nil
	}
	version, err := s.salesOrderDeliverySourceVersion(ctx)
	if err != nil {
		return err
	}
	var metadata struct {
		SourceVersion string `bson:"source_version"`
	}
	err = s.salesOrderDeliverySummaryMeta.FindOne(ctx, bson.M{"_id": "active"}).Decode(&metadata)
	if err != nil && err != mongo.ErrNoDocuments {
		return err
	}
	s.salesOrderDeliverySummaryStateMu.RLock()
	available := s.salesOrderDeliverySummaryAvailable
	s.salesOrderDeliverySummaryStateMu.RUnlock()
	if err == mongo.ErrNoDocuments || metadata.SourceVersion != version || !available {
		s.salesOrderDeliverySummaryStateMu.Lock()
		s.salesOrderDeliverySummaryAvailable = false
		s.salesOrderDeliverySummaryStateMu.Unlock()
		return s.RefreshSalesOrderDeliverySummary(ctx)
	}
	return nil
}

func (s *Store) markSalesOrderDeliverySummaryReady() {
	s.salesOrderDeliverySummaryReadyOnce.Do(func() {
		if s.salesOrderDeliverySummaryReady != nil {
			close(s.salesOrderDeliverySummaryReady)
		}
	})
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
	available := s.salesOrderDeliverySummaryAvailable
	s.salesOrderDeliverySummaryStateMu.RUnlock()
	if !available {
		return false, nil
	}
	active, err := s.salesOrderDeliverySyncActive(ctx)
	if err != nil {
		return false, err
	}
	if active {
		s.salesOrderDeliverySummaryStateMu.Lock()
		s.salesOrderDeliverySummaryAvailable = false
		s.salesOrderDeliverySummaryStateMu.Unlock()
		return false, nil
	}
	return true, nil
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
