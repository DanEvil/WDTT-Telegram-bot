package outbound

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// VKHashMode represents the mode for VK hash handling
type VKHashMode string

const (
	VKHashModeAuto    VKHashMode = "auto"
	VKHashModeFixed   VKHashMode = "fixed"
	VKHashModeManual  VKHashMode = "manual"
)

// Config holds the outbound plugin configuration
type Config struct {
	PanelURL              string     `json:"panel_url" validate:"required,url"`
	PanelUsername         string     `json:"panel_username" validate:"required"`
	PanelPassword         string     `json:"panel_password" validate:"required"`
	VerifySSL             bool       `json:"verify_ssl"`
	VKHashMode            VKHashMode `json:"vk_hash_mode" validate:"oneof=auto fixed manual"`
	FixedVKHash           string     `json:"fixed_vk_hash"`
	PoolSize              int        `json:"pool_size" validate:"min=1,max=1000"`
	VKHashRefreshInterval Duration   `json:"vk_hash_refresh_interval" validate:"required"`
}

// Duration wraps time.Duration to support JSON unmarshaling
type Duration struct {
	time.Duration
}

// UnmarshalJSON implements custom JSON unmarshaling for Duration
func (d *Duration) UnmarshalJSON(data []byte) error {
	var v interface{}
	if err := json.Unmarshal(data, &v); err != nil {
		return err
	}

	switch val := v.(type) {
	case string:
		dur, err := time.ParseDuration(val)
		if err != nil {
			return fmt.Errorf("invalid duration format: %w", err)
		}
		d.Duration = dur
	case float64:
		d.Duration = time.Duration(val) * time.Second
	default:
		return fmt.Errorf("duration must be a string or number")
	}
	return nil
}

// MarshalJSON implements custom JSON marshaling for Duration
func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.Duration.String())
}

// Validate validates the configuration
func (c *Config) Validate() error {
	if strings.TrimSpace(c.PanelURL) == "" {
		return fmt.Errorf("panel_url is required")
	}
	if strings.TrimSpace(c.PanelUsername) == "" {
		return fmt.Errorf("panel_username is required")
	}
	if strings.TrimSpace(c.PanelPassword) == "" {
		return fmt.Errorf("panel_password is required")
	}

	switch c.VKHashMode {
	case VKHashModeAuto, VKHashModeFixed, VKHashModeManual:
		// valid
	default:
		return fmt.Errorf("vk_hash_mode must be one of: auto, fixed, manual")
	}

	if c.VKHashMode == VKHashModeFixed && strings.TrimSpace(c.FixedVKHash) == "" {
		return fmt.Errorf("fixed_vk_hash is required when vk_hash_mode is 'fixed'")
	}

	if c.PoolSize < 1 {
		return fmt.Errorf("pool_size must be at least 1")
	}
	if c.PoolSize > 1000 {
		return fmt.Errorf("pool_size must not exceed 1000")
	}

	if c.VKHashRefreshInterval.Duration <= 0 {
		return fmt.Errorf("vk_hash_refresh_interval must be positive")
	}

	return nil
}

// LoadConfig loads configuration from JSON bytes
func LoadConfig(data []byte) (*Config, error) {
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("failed to unmarshal config: %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("config validation failed: %w", err)
	}
	return &cfg, nil
}

// DefaultConfig returns a configuration with sensible defaults
func DefaultConfig() *Config {
	return &Config{
		VerifySSL:             true,
		VKHashMode:            VKHashModeAuto,
		PoolSize:              10,
		VKHashRefreshInterval: Duration{Duration: 5 * time.Minute},
	}
}