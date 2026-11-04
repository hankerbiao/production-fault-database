package store

import "testing"

func TestSCSStreamIncludesDOAPlannedDate(t *testing.T) {
	fields := scsStreamFields("SCS_DOA")
	for _, want := range []string{"GSTRS", "gstrs_status", "production_orders"} {
		found := false
		for _, field := range fields {
			if field == want {
				found = true
			}
		}
		if !found {
			t.Errorf("missing %s in SCS stream", want)
		}
	}
}
