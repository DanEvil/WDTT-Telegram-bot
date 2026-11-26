package panel

import (
	"encoding/json"
)

// Config represents the panel configuration for WDTT outbound.
type Config struct {
	ListenAddr string `json:"listen_addr"`
	Protocol   string `json:"protocol"`
	Settings   json.RawMessage `json:"settings"`
}

// DefaultConfig returns a default configuration.
func DefaultConfig() *Config {
	return &Config{
		ListenAddr: "127.0.0.1:0",
		Protocol:   "wdtt",
	}
}