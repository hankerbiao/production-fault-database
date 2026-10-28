package main

import (
	"bytes"
	"crypto/md5"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

const sapMethod = "ZSIMS_CL_INBOUND_MO_PSINFO"

var sapSources = map[string]string{
	"SG": "http://10.2.101.37:8000/sap/ZHTTP_SIMS?sap-client=800",
	"KK": "http://10.8.100.11:8001/sap/ZHTTP_SIMS?sap-client=600",
}
var sapHTTPClient = &http.Client{Timeout: 120 * time.Second, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}

type sapQuery struct {
	Source string `json:"source"`
	Start  string `json:"start"`
	End    string `json:"end"`
	Order  string `json:"order"`
}

func (s *server) querySAP(w http.ResponseWriter, r *http.Request) {
	var q sapQuery
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&q); err != nil {
		writeJSON(w, 400, map[string]string{"error": "请求参数格式有误"})
		return
	}
	url, ok := sapSources[q.Source]
	start, err1 := time.Parse("2006-01-02", q.Start)
	end, err2 := time.Parse("2006-01-02", q.End)
	if !ok || err1 != nil || err2 != nil || end.Before(start) || end.Sub(start) > 30*24*time.Hour {
		writeJSON(w, 400, map[string]string{"error": "请选择 KK 或 SG，日期范围须有效且不超过 31 天"})
		return
	}
	order := strings.TrimSpace(q.Order)
	if len(order) > 40 {
		writeJSON(w, 400, map[string]string{"error": "生产订单号过长"})
		return
	}
	rows := make([]map[string]any, 0)
	fetched := 0
	for from := start; !from.After(end); from = from.AddDate(0, 0, 7) {
		to := from.AddDate(0, 0, 6)
		if to.After(end) {
			to = end
		}
		body, _ := json.Marshal(map[string]any{"CHDAT": from.Format("2006-01-02"), "CHDAT_TO": to.Format("2006-01-02"), "PRODH_LIST": []string{"00100"}})
		req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			writeJSON(w, 502, map[string]string{"error": "无法创建 SAP 请求"})
			return
		}
		location := time.FixedZone("CST", 8*60*60)
		today := time.Now().In(location).Format("20060102")
		sig := md5.Sum([]byte("sugon" + sapMethod + today + "sugon"))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("method", sapMethod)
		req.Header.Set("time", today)
		req.Header.Set("sign", fmt.Sprintf("%X", sig))
		resp, err := sapHTTPClient.Do(req)
		if err != nil {
			writeJSON(w, 502, map[string]string{"error": "SAP 来源连接失败，请检查网络或重试"})
			return
		}
		data, readErr := io.ReadAll(io.LimitReader(resp.Body, 16*1024*1024+1))
		resp.Body.Close()
		if readErr != nil || len(data) > 16*1024*1024 || resp.StatusCode != 200 {
			writeJSON(w, 502, map[string]string{"error": "SAP 响应失败或数据过大"})
			return
		}
		var result struct {
			Type    string          `json:"MSGTY"`
			Message string          `json:"MSGTX"`
			Data    json.RawMessage `json:"DATA"`
		}
		if json.Unmarshal(data, &result) != nil {
			writeJSON(w, 502, map[string]string{"error": "SAP 响应格式无效"})
			return
		}
		if kind := strings.ToUpper(result.Type); kind == "E" || kind == "A" || kind == "X" {
			if strings.Contains(strings.ToLower(result.Message), "no data") || strings.Contains(result.Message, "无数据") {
				continue
			}
			writeJSON(w, 502, map[string]string{"error": "SAP 返回错误：" + result.Message})
			return
		}
		var batch []map[string]any
		if len(result.Data) == 0 || string(result.Data) == "null" || json.Unmarshal(result.Data, &batch) != nil || batch == nil {
			writeJSON(w, 502, map[string]string{"error": "SAP 响应缺少有效的数据列表"})
			return
		}
		fetched += len(batch)
		for _, row := range batch {
			if order == "" || normalizeSAPOrder(fmt.Sprint(row["AUFNR"])) == normalizeSAPOrder(order) {
				rows = append(rows, row)
			}
		}
	}
	writeJSON(w, 200, map[string]any{"source": q.Source, "fetched": fetched, "items": rows})
}

func normalizeSAPOrder(value string) string {
	value = strings.TrimSpace(value)
	trimmed := strings.TrimLeft(value, "0")
	if trimmed == "" && value != "" {
		return "0"
	}
	return trimmed
}
