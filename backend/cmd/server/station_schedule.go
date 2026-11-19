package main

import (
	"context"
	"log"
	"time"

	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

type syncScheduleStore interface {
	ClaimSyncScheduleSlot(context.Context, string, string, time.Time) (bool, error)
	ReleaseSyncScheduleSlot(context.Context, string) error
}

var dashboardSyncHours = [...]int{8, 12, 18, 22}

func nextStationHourlySyncAt(now time.Time) time.Time {
	location, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		location = time.FixedZone("CST", 8*60*60)
	}
	local := now.In(location)
	return time.Date(local.Year(), local.Month(), local.Day(), local.Hour()+1, 0, 0, 0, location)
}

func nextLocalStationSyncAt(now time.Time) time.Time {
	return nextLocalHanaCronAt(now, 0, 5)
}

func nextLocalRepairSyncAt(now time.Time) time.Time {
	return nextLocalHanaCronAt(now, 1, 25)
}

func nextLocalOrderBOMSyncAt(now time.Time) time.Time {
	return nextLocalHanaCronAt(now, 0, 45)
}

func nextLocalHanaCronAt(now time.Time, hourParity, minute int) time.Time {
	location, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		location = time.FixedZone("CST", 8*60*60)
	}
	local := now.In(location)
	next := time.Date(local.Year(), local.Month(), local.Day(), local.Hour(), minute, 0, 0, location)
	if next.Hour()%2 != hourParity {
		next = next.Add(time.Hour)
	}
	if !next.After(local) {
		next = next.Add(2 * time.Hour)
	}
	return next
}

func nextSalesOrderDependencySyncAt(now time.Time) time.Time {
	next := nextLocalRepairSyncAt(now)
	if bom := nextLocalOrderBOMSyncAt(now); bom.Before(next) {
		next = bom
	}
	if dashboard := nextDashboardSyncAt(now); dashboard.Before(next) {
		return dashboard
	}
	return next
}

func nextDashboardSyncAt(now time.Time) time.Time {
	location, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		location = time.FixedZone("CST", 8*60*60)
	}
	local := now.In(location)
	for day := 0; day <= 1; day++ {
		date := local.AddDate(0, 0, day)
		for _, hour := range dashboardSyncHours {
			candidate := time.Date(date.Year(), date.Month(), date.Day(), hour, 0, 0, 0, location)
			if candidate.After(local) {
				return candidate
			}
		}
	}
	return time.Date(local.Year(), local.Month(), local.Day()+1, 8, 0, 0, 0, location)
}

func nextSCSSyncAt(now time.Time) time.Time {
	location, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		location = time.FixedZone("CST", 8*60*60)
	}
	local := now.In(location)
	next := time.Date(local.Year(), local.Month(), local.Day(), 18, 0, 0, 0, location)
	if !next.After(local) {
		next = next.AddDate(0, 0, 1)
	}
	return next
}

func scheduledSyncPlan(hour int) ([]string, string, string) {
	if isDashboardSyncHour(hour) {
		tasks := []string{"sales_orders"}
		if hour == 18 {
			tasks = append(tasks, "scs_doa", "scs_change")
		}
		return tasks, "all_dashboards", "dashboard_schedule"
	}
	return nil, "", ""
}

func (s *server) schedulePeriodicSync(ctx context.Context) {
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	location, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		log.Printf("加载工位定时同步时区失败: %v", err)
		return
	}
	lastStartedSlot := ""
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			now := time.Now().In(location)
			slot := now.Format("2006-01-02-15")
			if slot == lastStartedSlot {
				continue
			}
			if s.startScheduledSync(ctx, slot, now.Hour()) {
				lastStartedSlot = slot
			}
		}
	}
}

func (s *server) startScheduledSync(ctx context.Context, slot string, hour int) bool {
	taskIDs, job, source := scheduledSyncPlan(hour)
	if len(taskIDs) == 0 {
		return true
	}
	latest, err := s.store.LatestSyncRun(ctx)
	if err == nil {
		if bsonState := latest["state"]; bsonState == "running" {
			heartbeat, heartbeatErr := time.Parse(time.RFC3339, timeValue(latest["heartbeat_at"]))
			if heartbeatErr == nil && time.Since(heartbeat) <= syncRunStaleAfter() {
				log.Printf("跳过工位小时同步：已有编排任务运行中 slot=%s", slot)
				return false
			}
			log.Printf("忽略过期的同步运行记录并继续调度 slot=%s", slot)
		}
	} else if err != mongo.ErrNoDocuments {
		log.Printf("读取同步状态失败，跳过工位小时同步: slot=%s err=%v", slot, err)
		return false
	}
	claimer, ok := s.store.(syncScheduleStore)
	if !ok {
		log.Printf("定时同步存储不支持调度租约")
		return false
	}
	claimed, err := claimer.ClaimSyncScheduleSlot(ctx, "sync-schedule:"+slot, job, time.Now().UTC())
	if err != nil {
		log.Printf("领取工位小时同步时段失败: slot=%s err=%v", slot, err)
		return false
	}
	if !claimed {
		return true
	}
	_, err = s.sync.startRun(syncRunRequest{
		Mode: "incremental", TaskIDs: taskIDs, Source: source,
	})
	if err != nil {
		log.Printf("启动定时同步失败: job=%s slot=%s err=%v", job, slot, err)
		if releaseErr := claimer.ReleaseSyncScheduleSlot(ctx, "sync-schedule:"+slot); releaseErr != nil {
			log.Printf("释放失败的同步时段失败: slot=%s err=%v", slot, releaseErr)
		}
		return false
	}
	return true
}

func syncRunStaleAfter() time.Duration {
	seconds := getenv("SYNC_ORCHESTRATOR_STALE_SECONDS", "300")
	value, err := time.ParseDuration(seconds + "s")
	if err != nil || value <= 0 {
		return 5 * time.Minute
	}
	return value
}

func isDashboardSyncHour(hour int) bool {
	for _, scheduled := range dashboardSyncHours {
		if scheduled == hour {
			return true
		}
	}
	return false
}

var _ syncScheduleStore = (*store.Store)(nil)
