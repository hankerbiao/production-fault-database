package syncmanifest

import (
	_ "embed"
	"encoding/json"
)

//go:embed tasks.json
var data []byte

type Task struct {
	ID            string   `json:"id"`
	Label         string   `json:"label"`
	Dependencies  []string `json:"dependencies"`
	FullSupported bool     `json:"fullSupported"`
}

func Tasks() []Task {
	var tasks []Task
	if err := json.Unmarshal(data, &tasks); err != nil {
		panic(err)
	}
	return tasks
}
