package store

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

const MaxExportKeys = 1000

var ErrExportBatchTooLarge = errors.New("export batch exceeds 10000 rows; split the keys and retry")

var exportFaultFields = []string{
	"FACTORY", "VBELN", "AUFNR", "ZJXMC", "PCODE", "ZNGGZ", "U_FIND", "T_FIND",
	"ZNGWD3", "ZNGWD", "U_FIX", "ZWXDT", "ZWXWD3", "SECFLG", "ZBJ", "ZZRFL",
	"ZCCLH", "SECOND_PART_NO", "FIX_REMARKS", "MATNR", "MAKTX", "ZMCOD1", "ZRCOD1",
	"ZMCOD2", "ZRCOD2", "RECORD01REPAIRM", "ERROR_CODE", "ERROR_MSG", "REJUDGE",
	"RET", "RPDESC", "RNOTE", "U_RMA_NAM", "RMA_RESULT",
}

func exportProjection(fields []string) bson.M {
	projection := bson.M{"_id": 1, "_source_key": 1}
	for _, field := range fields {
		projection[field] = 1
	}
	return projection
}

func exportOrderKeys(keys []string) []string {
	result := []string{}
	seen := map[string]bool{}
	for _, key := range keys {
		for _, value := range orderCandidates(key, productionOrderWidth) {
			if !seen[value] {
				seen[value] = true
				result = append(result, value)
			}
		}
	}
	return result
}

// ExportRows reads source fields in a single bounded cursor, without counting
// and sorting the same result again for each 100-row browser page.
func (s *Store) ExportRows(ctx context.Context, resource string, keys []string) ([]bson.M, error) {
	if len(keys) > MaxExportKeys {
		return nil, fmt.Errorf("too many export keys")
	}
	if len(keys) == 0 {
		return []bson.M{}, nil
	}
	var collection *mongo.Collection
	var filter, projection bson.M
	switch resource {
	case "orders":
		collection = s.orders
		filter = bson.M{"_id": bson.M{"$in": keys}}
		projection = exportProjection([]string{"source", "aufnr", "data"})
	case "faults":
		collection = s.repairs
		filter = bson.M{"AUFNR": bson.M{"$in": exportOrderKeys(keys)}}
		projection = exportProjection(exportFaultFields)
	case "bindings":
		collection = s.views["ZSGV_ZPP_SERNOLIST"]
		filter = bson.M{"AUFNR_HEAD": bson.M{"$in": exportOrderKeys(keys)}}
		projection = exportProjection([]string{"AUFNR_HEAD", "AUFNR_ITEM", "ZCODE_HEAD", "ZCODE_ITEM"})
	default:
		return nil, fmt.Errorf("unknown export resource")
	}
	cursor, err := collection.Find(ctx, filter, options.Find().
		SetProjection(projection).SetLimit(MaxBulkQueryRows+1).SetBatchSize(1000))
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)
	rows := []bson.M{}
	if err := cursor.All(ctx, &rows); err != nil {
		return nil, err
	}
	if len(rows) > MaxBulkQueryRows {
		return nil, ErrExportBatchTooLarge
	}
	return rows, nil
}

// ExportVersion is a cheap invalidation token, not a business row count.
func (s *Store) ExportVersion(ctx context.Context) (map[string]string, error) {
	resources := []struct {
		name       string
		collection *mongo.Collection
		timeField  string
	}{
		{"orders", s.orders, "last_synced_at"},
		{"faults", s.repairs, "_synced_at"},
		{"bindings", s.views["ZSGV_ZPP_SERNOLIST"], "_synced_at"},
	}
	result := map[string]string{}
	var lock sync.Mutex
	var once sync.Once
	var resultErr error
	var jobs sync.WaitGroup
	for _, resource := range resources {
		jobs.Add(1)
		go func() {
			defer jobs.Done()
			var row bson.M
			err := resource.collection.FindOne(ctx, bson.M{}, options.FindOne().
				SetSort(bson.D{{Key: resource.timeField, Value: -1}}).
				SetProjection(bson.M{resource.timeField: 1})).Decode(&row)
			if err != nil && err != mongo.ErrNoDocuments {
				once.Do(func() { resultErr = err })
				return
			}
			count, err := resource.collection.EstimatedDocumentCount(ctx)
			if err != nil {
				once.Do(func() { resultErr = err })
				return
			}
			lock.Lock()
			result[resource.name] = fmt.Sprintf("%d:%s", count, timeText(row[resource.timeField]))
			lock.Unlock()
		}()
	}
	jobs.Wait()
	return result, resultErr
}
