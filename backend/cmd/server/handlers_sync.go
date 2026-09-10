package main

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"production-fault-gateway/internal/store"
)

func decodeSyncRequest(r *http.Request) (syncRunRequest, error) {
	var request syncRunRequest
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		return request, err
	}
	if request.Mode == "" {
		request.Mode = "incremental"
	}
	for _, value := range []struct{ name, date string }{{"startDate", request.StartDate}, {"endDate", request.EndDate}} {
		if value.date != "" {
			if _, err := time.Parse("2006-01-02", value.date); err != nil {
				return request, &syncRequestError{message: value.name + " 必须为 YYYY-MM-DD"}
			}
		}
	}
	return request, nil
}

type syncRequestError struct{ message string }

func (e *syncRequestError) Error() string { return e.message }

func (s *server) startSyncRun(w http.ResponseWriter, r *http.Request) {
	request, err := decodeSyncRequest(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	if request.Mode == "incremental" && request.StartDate != "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "增量同步不接受 startDate"})
		return
	}
	if request.StartDate != "" && request.EndDate != "" && request.StartDate > request.EndDate {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "startDate 不能晚于 endDate"})
		return
	}
	if status, active, lookupErr := s.activeSyncRun(r); lookupErr != nil {
		writeStoreError(w, lookupErr)
		return
	} else if active {
		writeJSON(w, http.StatusConflict, map[string]any{"error": "已有同步任务运行中", "status": status})
		return
	}
	status, err := s.sync.startRun(request)
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "status": status})
		return
	}
	writeJSON(w, http.StatusAccepted, status)
}

func (s *server) startIncrementalSync(w http.ResponseWriter, r *http.Request) {
	if status, active, lookupErr := s.activeSyncRun(r); lookupErr != nil {
		writeStoreError(w, lookupErr)
		return
	} else if active {
		writeJSON(w, http.StatusConflict, map[string]any{"error": "已有同步任务运行中", "status": status})
		return
	}
	status, err := s.sync.startRun(syncRunRequest{Mode: "incremental"})
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "status": status})
		return
	}
	writeJSON(w, http.StatusAccepted, status)
}

func (s *server) activeSyncRun(r *http.Request) (syncStatus, bool, error) {
	run, err := s.store.LatestSyncRun(r.Context())
	if err == mongo.ErrNoDocuments {
		return syncStatus{}, false, nil
	}
	if err != nil {
		return syncStatus{}, false, err
	}
	status := syncStatusFromRun(run)
	return status, status.State == "running", nil
}

func syncStatusFromRun(run store.SyncRun) syncStatus {
	data := bson.M(run)
	return syncStatus{RunID: textValue(data["_id"]), State: textValue(data["state"]), Mode: textValue(data["mode"]), StartedAt: timeValue(data["started_at"]), FinishedAt: timeValue(data["finished_at"]), Message: textValue(data["message"]), Summary: mapValue(data)}
}

func textValue(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func timeValue(value any) string {
	if instant, ok := value.(time.Time); ok {
		return instant.UTC().Format(time.RFC3339)
	}
	return ""
}

func mapValue(value any) map[string]any {
	if result, ok := value.(bson.M); ok {
		return result
	}
	if result, ok := value.(map[string]any); ok {
		return result
	}
	return nil
}

func (s *server) syncStatus(w http.ResponseWriter, r *http.Request) {
	run, err := s.store.LatestSyncRun(r.Context())
	if err == nil {
		writeJSON(w, http.StatusOK, syncStatusFromRun(run))
		return
	}
	if err != mongo.ErrNoDocuments {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s.sync.statusSnapshot())
}

func (s *server) syncRun(w http.ResponseWriter, r *http.Request) {
	run, err := s.store.SyncRun(r.Context(), r.PathValue("id"))
	if err == mongo.ErrNoDocuments {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "同步运行不存在"})
		return
	}
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, run)
}

func (s *server) syncRuns(w http.ResponseWriter, r *http.Request) {
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	runs, err := s.store.SyncRuns(r.Context(), limit)
	if err != nil {
		writeStoreError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": runs})
}

func (s *server) syncTasks(w http.ResponseWriter, r *http.Request) {
	stateByTask := map[string]string{}
	if latest, err := s.store.LatestSyncRun(r.Context()); err == nil {
		if stages := anySlice(bson.M(latest)["stages"]); stages != nil {
			for _, stage := range stages {
				if row, ok := stage.(bson.M); ok {
					stateByTask[textValue(row["task_id"])] = textValue(row["state"])
				}
			}
		}
	}
	tasks := make([]map[string]any, len(managedSyncTasks))
	for index, task := range managedSyncTasks {
		tasks[index] = map[string]any{"id": task.ID, "label": task.Label, "dependencies": task.Dependencies, "fullSupported": task.FullSupported, "state": stateByTask[task.ID]}
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": tasks})
}

func listStrings(value any) []string {
	values := anySlice(value)
	if values == nil {
		return nil
	}
	items := make([]string, 0, len(values))
	for _, value := range values {
		if text := textValue(value); text != "" {
			items = append(items, text)
		}
	}
	return items
}

func anySlice(value any) []any {
	switch values := value.(type) {
	case []any:
		return values
	case bson.A:
		return []any(values)
	default:
		return nil
	}
}

func (s *server) retrySyncRun(w http.ResponseWriter, r *http.Request) {
	run, err := s.store.SyncRun(r.Context(), r.PathValue("id"))
	if err == mongo.ErrNoDocuments {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "同步运行不存在"})
		return
	}
	if err != nil {
		writeStoreError(w, err)
		return
	}
	data := bson.M(run)
	request := syncRunRequest{Mode: textValue(data["mode"]), TaskIDs: listStrings(data["requested_task_ids"]), StartDate: textValue(data["start_date"]), EndDate: textValue(data["end_date"])}
	status, err := s.sync.startRun(request)
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"error": err.Error(), "status": status})
		return
	}
	writeJSON(w, http.StatusAccepted, status)
}

func (s *server) dataStatus(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.DataStatus(r.Context())
	if err != nil {
		writeStoreError(w, err)
		return
	}
	status := s.sync.statusSnapshot()
	if run, runErr := s.store.LatestSyncRun(r.Context()); runErr == nil {
		status = syncStatusFromRun(run)
	}
	writeJSON(w, http.StatusOK, map[string]any{"salesOrdersLastSyncedAt": result.SalesOrdersLastSyncedAt, "faultsLastSyncedAt": result.FaultsLastSyncedAt, "stationRecordsLastSyncedAt": result.StationRecordsLastSyncedAt, "serialBindingsLastSyncedAt": result.SerialBindingsLastSyncedAt, "bomPostingsLastSyncedAt": result.BOMPostingsLastSyncedAt, "scsDoaLastSyncedAt": result.SCSDoaLastSyncedAt, "scsChangeLastSyncedAt": result.SCSChangeLastSyncedAt, "scsDoaRecordCount": result.SCSDoaRecordCount, "scsChangeRecordCount": result.SCSChangeRecordCount, "state": status.State, "startedAt": status.StartedAt, "finishedAt": status.FinishedAt})
}
