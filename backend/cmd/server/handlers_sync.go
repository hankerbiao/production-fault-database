package main

import "net/http"

func (s *server) startIncrementalSync(w http.ResponseWriter, r *http.Request) {
	status, err := s.sync.start()
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "status": status})
		return
	}
	writeJSON(w, http.StatusAccepted, status)
}

func (s *server) syncStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sync.statusSnapshot())
}

func (s *server) dataStatus(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.DataStatus(r.Context())
	if err != nil {
		writeStoreError(w, err)
		return
	}
	status := s.sync.statusSnapshot()
	writeJSON(w, http.StatusOK, map[string]any{"salesOrdersLastSyncedAt": result.SalesOrdersLastSyncedAt, "faultsLastSyncedAt": result.FaultsLastSyncedAt, "stationRecordsLastSyncedAt": result.StationRecordsLastSyncedAt, "serialBindingsLastSyncedAt": result.SerialBindingsLastSyncedAt, "bomPostingsLastSyncedAt": result.BOMPostingsLastSyncedAt, "state": status.State, "startedAt": status.StartedAt, "finishedAt": status.FinishedAt})
}
