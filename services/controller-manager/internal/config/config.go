package config

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

type ModelConfig struct {
	ComposeFile   string   `yaml:"compose_file"`
	Services      []string `yaml:"services,omitempty"`       // targeted services (empty = entire compose)
	HealthURL     string   `yaml:"health_url"`
	HealthTimeout string   `yaml:"health_timeout,omitempty"` // e.g. "660s", "5m" — default 300s
	Alias         string   `yaml:"alias"`
}

// HealthTimeoutDuration parses HealthTimeout or returns the default 300s.
func (m ModelConfig) HealthTimeoutDuration() time.Duration {
	if m.HealthTimeout == "" {
		return 300 * time.Second
	}
	d, err := time.ParseDuration(m.HealthTimeout)
	if err != nil {
		return 300 * time.Second
	}
	return d
}

type Config struct {
	Models map[string]ModelConfig `yaml:"models"`
}

func Load(path string) (*Config, error) {
	if path == "" {
		path = "config/models.yaml"
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config %s: %w", path, err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config %s: %w", path, err)
	}
	if len(cfg.Models) == 0 {
		return nil, fmt.Errorf("config %s: no models defined", path)
	}
	return &cfg, nil
}
