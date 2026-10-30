package store

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"go.mongodb.org/mongo-driver/bson"
)

func (s *Store) ViewOptions(ctx context.Context, viewID, field string) (ViewOptionsResult, error) {
	if viewID != "ZSGV_ZSD124" || !strings.EqualFold(strings.TrimSpace(field), "cpx") {
		return ViewOptionsResult{}, fmt.Errorf("仅支持订单 BOM 过账视图的 cpx 选项")
	}

	cacheKey := viewID + ":cpx"
	s.viewOptionsCacheMu.Lock()
	if cached, ok := s.viewOptionsCache[cacheKey]; ok && time.Since(s.viewOptionsCacheAt) < viewOptionsCacheTTL {
		s.viewOptionsCacheMu.Unlock()
		return ViewOptionsResult{Items: append([]string(nil), cached.Items...)}, nil
	}
	s.viewOptionsCacheMu.Unlock()

	filter := bson.M{"CPX": bson.M{"$exists": true, "$nin": bson.A{"", nil}}}
	values, err := s.views[viewID].Distinct(ctx, "CPX", filter)
	if err != nil {
		return ViewOptionsResult{}, err
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
	result := ViewOptionsResult{Items: items}
	s.viewOptionsCacheMu.Lock()
	s.viewOptionsCache[cacheKey] = ViewOptionsResult{Items: append([]string(nil), items...)}
	s.viewOptionsCacheAt = time.Now()
	s.viewOptionsCacheMu.Unlock()
	return result, nil
}
