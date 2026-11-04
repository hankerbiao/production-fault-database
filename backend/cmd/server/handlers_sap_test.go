package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestSAPQueryWindowsAndOrderFilter(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload["PRODH_LIST"].([]any)[0] != "00100" || r.Header.Get("method") != sapMethod {
			t.Error("unexpected SAP request")
		}
		if calls == 1 && (payload["CHDAT"] != "2026-01-01" || payload["CHDAT_TO"] != "2026-01-07") {
			t.Errorf("unexpected first window: %v", payload)
		}
		if calls == 2 && (payload["CHDAT"] != "2026-01-08" || payload["CHDAT_TO"] != "2026-01-08") {
			t.Errorf("unexpected second window: %v", payload)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"DATA":[{"AUFNR":"00030222644"},{"AUFNR":"123"}]}`))
	}))
	defer upstream.Close()
	previous := sapSources["KK"]
	sapSources["KK"] = upstream.URL
	defer func() { sapSources["KK"] = previous }()
	req := httptest.NewRequest("POST", "/api/sap/query", strings.NewReader(`{"source":"KK","start":"2026-01-01","end":"2026-01-08","order":"30222644"}`))
	response := httptest.NewRecorder()
	(&server{}).querySAP(response, req)
	var result struct {
		Fetched int              `json:"fetched"`
		Items   []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if response.Code != 200 || calls != 2 || result.Fetched != 4 || len(result.Items) != 2 {
		t.Fatalf("unexpected response: %d %s", response.Code, response.Body.String())
	}
}

func TestSAPQueryRejectsInvalidRequestsAndUpstreamErrors(t *testing.T) {
	for _, body := range []string{`{"source":"SK","start":"2026-01-01","end":"2026-01-02"}`, `{"source":"KK","start":"2026-01-01","end":"2026-02-01"}`} {
		w := httptest.NewRecorder()
		(&server{}).querySAP(w, httptest.NewRequest("POST", "/api/sap/query", strings.NewReader(body)))
		if w.Code != 400 {
			t.Fatalf("expected bad request, got %d", w.Code)
		}
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"MSGTY":"E","MSGTX":"broken"}`)) }))
	defer upstream.Close()
	previous := sapSources["SG"]
	sapSources["SG"] = upstream.URL
	defer func() { sapSources["SG"] = previous }()
	w := httptest.NewRecorder()
	(&server{}).querySAP(w, httptest.NewRequest("POST", "/api/sap/query", strings.NewReader(`{"source":"SG","start":"2026-01-01","end":"2026-01-01"}`)))
	if w.Code != 502 {
		t.Fatalf("expected upstream error, got %d", w.Code)
	}
}
