package main

import (
	"testing"
	"time"
)

func TestScheduledSyncPlanLeavesHANATasksToLocalCron(t *testing.T) {
	for hour := 0; hour < 24; hour++ {
		tasks, job, source := scheduledSyncPlan(hour)
		counts := map[string]int{}
		for _, task := range tasks {
			counts[task]++
		}
		if counts["repair_records"] != 0 || counts["station_records_sales_orders"] != 0 || counts["order_bom_postings"] != 0 || counts["order_bom_planned_start"] != 0 {
			t.Fatalf("hour %d: scheduled plan must not include HANA tasks: %v", hour, tasks)
		}
		wantSCS := 0
		if hour == 18 {
			wantSCS = 1
		}
		if counts["scs_doa"] != wantSCS || counts["scs_change"] != wantSCS {
			t.Fatalf("hour %d: unexpected SCS plan: %v", hour, tasks)
		}
		if isDashboardSyncHour(hour) {
			if source != "dashboard_schedule" || job != "all_dashboards" || counts["sales_orders"] != 1 {
				t.Fatalf("hour %d: dashboard plan changed: %v", hour, tasks)
			}
		} else if len(tasks) != 0 || source != "" || job != "" {
			t.Fatalf("hour %d: expected no server scheduled tasks, got %v", hour, tasks)
		}
	}
}

func TestNextSCSSyncAt(t *testing.T) {
	shanghai := time.FixedZone("CST", 8*60*60)
	for _, test := range []struct{ now, want string }{
		{"2026-11-10T17:59:00+08:00", "2026-11-10T18:00:00+08:00"},
		{"2026-11-10T18:00:00+08:00", "2026-11-11T18:00:00+08:00"},
	} {
		now, err := time.Parse(time.RFC3339, test.now)
		if err != nil {
			t.Fatal(err)
		}
		got := nextSCSSyncAt(now.In(shanghai)).Format(time.RFC3339)
		if got != test.want {
			t.Fatalf("now %s: got %s, want %s", test.now, got, test.want)
		}
	}
}

func TestNextLocalStationSyncAt(t *testing.T) {
	for _, tc := range []struct{ now, want string }{
		{"2026-11-10T12:04:00+08:00", "2026-11-10T12:05:00+08:00"},
		{"2026-11-10T12:05:00+08:00", "2026-11-10T14:05:00+08:00"},
		{"2026-11-10T13:10:00+08:00", "2026-11-10T14:05:00+08:00"},
	} {
		now, err := time.Parse(time.RFC3339, tc.now)
		if err != nil {
			t.Fatal(err)
		}
		if got := nextLocalStationSyncAt(now).Format(time.RFC3339); got != tc.want {
			t.Fatalf("now %s: got %s, want %s", tc.now, got, tc.want)
		}
	}
}
