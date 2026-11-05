// hana-diagnose directly runs the station query using go-hdb, without a gateway.
package main

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"
	"time"

	_ "github.com/SAP/go-hdb/driver"
)

func main() {
	from := flag.String("from", "2026-10-30", "start date (YYYY-MM-DD)")
	to := flag.String("to", "2026-11-05", "end date (YYYY-MM-DD)")
	flag.Parse()
	config := map[string]string{}
	for _, k := range []string{"HANA_ADDRESS", "HANA_PORT", "HANA_USER", "HANA_PASSWORD"} {
		config[k] = os.Getenv(k)
	}
	// Optionally read configuration on stdin, avoiding credentials in process arguments.
	if st, _ := os.Stdin.Stat(); st.Mode()&os.ModeCharDevice == 0 {
		scanner := bufio.NewScanner(os.Stdin)
		for scanner.Scan() {
			k, v, ok := strings.Cut(scanner.Text(), "=")
			if ok {
				k = strings.TrimSpace(strings.TrimPrefix(k, "export "))
				if _, exists := config[k]; exists && config[k] == "" {
					config[k] = strings.Trim(strings.TrimSpace(v), "\"'")
				}
			}
		}
	}
	if config["HANA_PORT"] == "" {
		config["HANA_PORT"] = "30015"
	}
	out := map[string]any{"dateFrom": *from, "dateTo": *to, "target": net.JoinHostPort(config["HANA_ADDRESS"], config["HANA_PORT"]), "driver": "go-hdb", "rows": 0, "complete": false}
	if config["HANA_ADDRESS"] == "" || config["HANA_USER"] == "" || config["HANA_PASSWORD"] == "" {
		out["error"] = "HANA_ADDRESS, HANA_USER and HANA_PASSWORD required"
		emit(out, 1)
	}
	dsn := url.URL{Scheme: "hdb", User: url.UserPassword(config["HANA_USER"], config["HANA_PASSWORD"]), Host: net.JoinHostPort(config["HANA_ADDRESS"], config["HANA_PORT"]), RawQuery: "timeout=120&pingInterval=30"}
	db, err := sql.Open("hdb", dsn.String())
	if err != nil {
		out["error"] = err.Error()
		emit(out, 1)
	}
	defer db.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()
	query := `SELECT "MANDT","HISTROYID","PCODE","OCODE","AUFNR","KDAUF","KDPOS","LGORT","MAKTX_TH","LEAD_CYCLE","AUFNR_CYCLE","CUSTOMIZE_CYCLE","OEMBZ","SPEC","OPERATION","SPEC_DESC","UNAME","LASTSPEC_TIME","SPEC_TIME","GSTRS","PLAN_END_TIME","ACTUAL_START_TIME","ACTUAL_END_TIME","HADE1","HADE2","HADE3","HADE4","HADE5","HADE6","HADE7","HADE8","STATU","MESS" FROM "_SYS_BIC"."BW_LOCAL.ZTRRI/Z_V_ZMES_T_001" WHERE "GSTRS" >= ? AND "GSTRS" <= ? ORDER BY "ACTUAL_START_TIME" DESC, "HISTROYID" DESC, "SPEC_TIME" DESC, "PCODE" DESC, "SPEC" DESC, "OCODE" DESC LIMIT 500001`
	start := time.Now()
	rows, err := db.QueryContext(ctx, query, strings.ReplaceAll(*from, "-", ""), strings.ReplaceAll(*to, "-", ""))
	if err != nil {
		out["stage"] = "query"
		out["error"] = err.Error()
		out["seconds"] = time.Since(start).Seconds()
		emit(out, 1)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		out["error"] = err.Error()
		emit(out, 1)
	}
	models := map[string]bool{}
	count := 0
	for rows.Next() {
		vals := make([]sql.NullString, len(cols))
		dest := make([]any, len(cols))
		for i := range vals {
			dest[i] = &vals[i]
		}
		if err = rows.Scan(dest...); err != nil {
			break
		}
		count++
		for i, c := range cols {
			if c == "MAKTX_TH" {
				models[vals[i].String] = true
			}
		}
	}
	if err == nil {
		err = rows.Err()
	}
	out["seconds"] = time.Since(start).Seconds()
	out["rows"] = count
	out["distinctModels"] = len(models)
	if err != nil {
		out["stage"] = "read"
		out["error"] = err.Error()
		emit(out, 1)
	}
	if count > 500000 {
		out["error"] = "result exceeds gateway limit"
		emit(out, 1)
	}
	out["complete"] = true
	emit(out, 0)
}
func emit(v any, code int) {
	b, _ := json.MarshalIndent(v, "", "  ")
	fmt.Println(string(b))
	if code != 0 {
		os.Exit(code)
	}
}
