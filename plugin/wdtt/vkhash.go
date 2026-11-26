package wdtt

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// VKHashMode defines the hash management mode
type VKHashMode string

const (
	ModeAuto   VKHashMode = "auto"   // Poll panel API for hash
	ModeFixed  VKHashMode = "fixed"  // Use fixed hash from config
	ModeManual VKHashMode = "manual" // Hash provided externally
)

// UserVKHash holds a user's vk_hash with metadata
type UserVKHash struct {
	UserID    string    `json:"user_id"`
	VKHash    string    `json:"vk_hash"`
	ExpiresAt time.Time `json:"expires_at"`
}

// VKHashConfig holds configuration for the VK Hash manager
type VKHashConfig struct {
	Mode           VKHashMode      `json:"mode"`
	PollInterval   time.Duration   `json:"poll_interval"` // For auto mode
	FixedHash      string          `json:"fixed_hash"`      // For fixed mode
	PanelAPIURL    string          `json:"panel_api_url"`   // e.g., "https://panel.example.com/panel/api/users"
	HTTPClient     *http.Client    `json:"-"`
	TTL            time.Duration   `json:"ttl"`             // Cache TTL
	OnHashChange   func(string, string) `json:"-"`         // Callback: (userID, newHash)
}

// VKHashManager manages VK hashes with background polling
type VKHashManager struct {
	config   VKHashConfig
	cache    map[string]UserVKHash
	mu       sync.RWMutex
	stopCh   chan struct{}
	wg       sync.WaitGroup
	started  bool
	startMu  sync.Mutex
}

// NewVKHashManager creates a new VK Hash manager
func NewVKHashManager(config VKHashConfig) *VKHashManager {
	if config.HTTPClient == nil {
		config.HTTPClient = &http.Client{Timeout: 10 * time.Second}
	}
	if config.TTL == 0 {
		config.TTL = 5 * time.Minute
	}
	if config.PollInterval == 0 {
		config.PollInterval = 30 * time.Second
	}
	if config.PanelAPIURL == "" {
		config.PanelAPIURL = "https://panel.example.com/panel/api/users"
	}
	return &VKHashManager{
		config: config,
		cache:  make(map[string]UserVKHash),
		stopCh: make(chan struct{}),
	}
}

// Start begins the background poller (for auto mode)
func (m *VKHashManager) Start(ctx context.Context) error {
	m.startMu.Lock()
	defer m.startMu.Unlock()

	if m.started {
		return fmt.Errorf("VKHashManager already started")
	}

	if m.config.Mode != ModeAuto {
		return fmt.Errorf("Start() only valid in auto mode, current: %s", m.config.Mode)
	}

	m.started = true
	m.wg.Add(1)
	go m.pollLoop(ctx)
	return nil
}

// Stop gracefully shuts down the poller
func (m *VKHashManager) Stop() {
	m.startMu.Lock()
	if !m.started {
		m.startMu.Unlock()
		return
	}
	m.started = false
	m.startMu.Unlock()

	close(m.stopCh)
	m.wg.Wait()
}

// pollLoop runs the background polling
func (m *VKHashManager) pollLoop(ctx context.Context) {
	defer m.wg.Done()

	ticker := time.NewTicker(m.config.PollInterval)
	defer ticker.Stop()

	// Initial fetch
	m.fetchAndUpdate(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-m.stopCh:
			return
		case <-ticker.C:
			m.fetchAndUpdate(ctx)
		}
	}
}

// fetchAndUpdate queries the panel API and updates cache
func (m *VKHashManager) fetchAndUpdate(ctx context.Context) {
	req, err := http.NewRequestWithContext(ctx, "GET", m.config.PanelAPIURL, nil)
	if err != nil {
		return
	}

	resp, err := m.config.HTTPClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return
	}

	var users []UserVKHash
	if err := json.NewDecoder(resp.Body).Decode(&users); err != nil {
		return
	}

	now := time.Now()
	m.mu.Lock()
	defer m.mu.Unlock()

	for _, u := range users {
		if u.VKHash == "" {
			continue
		}

		old, exists := m.cache[u.UserID]
		if exists && old.VKHash == u.VKHash {
			// Hash unchanged, just refresh TTL
			old.ExpiresAt = now.Add(m.config.TTL)
			m.cache[u.UserID] = old
			continue
		}

		// Hash changed or new user
		u.ExpiresAt = now.Add(m.config.TTL)
		m.cache[u.UserID] = u

		if m.config.OnHashChange != nil && (!exists || old.VKHash != u.VKHash) {
			go m.config.OnHashChange(u.UserID, u.VKHash)
		}
	}
}

// GetVKHash returns the cached vk_hash for a user
func (m *VKHashManager) GetVKHash(userID string) (string, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	hash, exists := m.cache[userID]
	if !exists {
		return "", false
	}
	if time.Now().After(hash.ExpiresAt) {
		return "", false
	}
	return hash.VKHash, true
}

// SetFixedHash sets a fixed hash for a user (fixed/manual mode)
func (m *VKHashManager) SetFixedHash(userID, vkHash string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.cache[userID] = UserVKHash{
		UserID:    userID,
		VKHash:    vkHash,
		ExpiresAt: time.Now().Add(m.config.TTL),
	}
}

// SetManualHash sets a hash provided externally (manual mode)
func (m *VKHashManager) SetManualHash(userID, vkHash string) {
	m.SetFixedHash(userID, vkHash)
}

// RemoveUser removes a user's hash from cache
func (m *VKHashManager) RemoveUser(userID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.cache, userID)
}

// GetAllHashes returns all cached hashes (for debugging/inspection)
func (m *VKHashManager) GetAllHashes() map[string]UserVKHash {
	m.mu.RLock()
	defer m.mu.RUnlock()

	result := make(map[string]UserVKHash, len(m.cache))
	for k, v := range m.cache {
		result[k] = v
	}
	return result
}

// RotateHash gracefully rotates a user's hash: drains old connections, creates new
func (m *VKHashManager) RotateHash(userID, newHash string, drainFunc func(string) error) error {
	m.mu.Lock()
	oldHash, exists := m.cache[userID]
	if !exists {
		m.mu.Unlock()
		return fmt.Errorf("user %s not found in cache", userID)
	}

	// Update cache with new hash immediately
	m.cache[userID] = UserVKHash{
		UserID:    userID,
		VKHash:    newHash,
		ExpiresAt: time.Now().Add(m.config.TTL),
	}
	m.mu.Unlock()

	// Drain old connections using old hash
	if drainFunc != nil {
		if err := drainFunc(oldHash.VKHash); err != nil {
			// On drain failure, revert cache
			m.mu.Lock()
			m.cache[userID] = oldHash
			m.mu.Unlock()
			return fmt.Errorf("drain failed, reverted: %w", err)
		}
	}

	// Notify callback
	if m.config.OnHashChange != nil {
		go m.config.OnHashChange(userID, newHash)
	}

	return nil
}

// SetMode changes the hash management mode
func (m *VKHashManager) SetMode(mode VKHashMode) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	oldMode := m.config.Mode
	m.config.Mode = mode

	// If switching to auto mode, would need to restart poller
	// This is a simplified version - in production you'd handle restart
	if oldMode != ModeAuto && mode == ModeAuto {
		return fmt.Errorf("switching to auto mode requires restart")
	}
	return nil
}

// Stats returns cache statistics
func (m *VKHashManager) Stats() map[string]interface{} {
	m.mu.RLock()
	defer m.mu.RUnlock()

	expired := 0
	for _, v := range m.cache {
		if time.Now().After(v.ExpiresAt) {
			expired++
		}
	}
	return map[string]interface{}{
		"total":    len(m.cache),
		"expired":  expired,
		"active":   len(m.cache) - expired,
		"mode":     m.config.Mode,
		"started":  m.started,
	}
}