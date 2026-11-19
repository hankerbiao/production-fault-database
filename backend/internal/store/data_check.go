package store

import (
	"context"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

type DataCheckConfig struct {
	Enabled   bool      `bson:"enabled" json:"enabled"`
	DailyTime string    `bson:"daily_time" json:"dailyTime"`
	Timezone  string    `bson:"timezone" json:"timezone"`
	UpdatedAt time.Time `bson:"updated_at" json:"updatedAt,omitempty"`
}

func (s *Store) DataCheckConfig(ctx context.Context) (DataCheckConfig, error) {
	config := DataCheckConfig{Enabled: false, DailyTime: "03:00", Timezone: "Asia/Shanghai"}
	err := s.db.Collection("data_check_settings").FindOne(ctx, bson.M{"_id": "station_records_sales_orders"}).Decode(&config)
	if err == mongo.ErrNoDocuments {
		return config, nil
	}
	if err != nil {
		return DataCheckConfig{}, err
	}
	if config.DailyTime == "" {
		config.DailyTime = "03:00"
	}
	if config.Timezone == "" {
		config.Timezone = "Asia/Shanghai"
	}
	return config, nil
}

func (s *Store) SaveDataCheckConfig(ctx context.Context, config DataCheckConfig) error {
	config.Timezone = "Asia/Shanghai"
	config.UpdatedAt = time.Now().UTC()
	_, err := s.db.Collection("data_check_settings").UpdateOne(ctx,
		bson.M{"_id": "station_records_sales_orders"},
		bson.M{"$set": bson.M{"enabled": config.Enabled, "daily_time": config.DailyTime,
			"timezone": config.Timezone, "updated_at": config.UpdatedAt}},
		options.Update().SetUpsert(true))
	return err
}

func (s *Store) CreateDataCheckRun(ctx context.Context, run bson.M) error {
	_, err := s.db.Collection("data_check_runs").InsertOne(ctx, run)
	return err
}

func (s *Store) UpdateDataCheckRun(ctx context.Context, id string, fields bson.M) error {
	_, err := s.db.Collection("data_check_runs").UpdateOne(ctx, bson.M{"_id": id}, bson.M{"$set": fields})
	return err
}

func (s *Store) DataCheckRun(ctx context.Context, id string) (bson.M, error) {
	var run bson.M
	err := s.db.Collection("data_check_runs").FindOne(ctx, bson.M{"_id": id}).Decode(&run)
	return run, err
}

func (s *Store) DataCheckRuns(ctx context.Context, limit int) ([]bson.M, error) {
	if limit < 1 || limit > 100 {
		limit = 20
	}
	cursor, err := s.db.Collection("data_check_runs").Find(ctx, bson.M{},
		options.Find().SetSort(bson.D{{Key: "started_at", Value: -1}}).SetLimit(int64(limit)))
	if err != nil {
		return nil, err
	}
	defer cursor.Close(ctx)
	var runs []bson.M
	if err := cursor.All(ctx, &runs); err != nil {
		return nil, err
	}
	return runs, nil
}

// ClaimSyncScheduleSlot ensures only one server instance schedules a workflow
// for a given Shanghai calendar hour.
func (s *Store) ClaimSyncScheduleSlot(ctx context.Context, slot, job string, now time.Time) (bool, error) {
	_, err := s.db.Collection("sync_schedule_claims").InsertOne(ctx, bson.M{
		"_id": slot, "job": job, "created_at": now.UTC(),
	})
	if mongo.IsDuplicateKeyError(err) {
		return false, nil
	}
	return err == nil, err
}

func (s *Store) ReleaseSyncScheduleSlot(ctx context.Context, slot string) error {
	_, err := s.db.Collection("sync_schedule_claims").DeleteOne(ctx, bson.M{"_id": slot})
	return err
}
