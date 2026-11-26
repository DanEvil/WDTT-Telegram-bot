package wdtt

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/rs/zerolog"
)

// RelayState represents the current state of a VK relay.
type RelayState string

const (
	RelayStatePending  RelayState = "pending"
	RelayStateActive   RelayState = "active"
	RelayStateExpired  RelayState = "expired"
	RelayStateFailed   RelayState = "failed"
)

// Relay represents a single VK relay instance.
type Relay struct {
	ID          string
	UserID      string
	VKHash      string
	Endpoint    string
	State       RelayState
	CreatedAt   time.Time
	ExpiresAt   time.Time
	LastHealth  time.Time
	FailCount   int
	mu          sync.RWMutex
}

// IsExpired checks if the relay has expired.
func (r *Relay) IsExpired() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return time.Now().After(r.ExpiresAt)
}

// IsHealthy checks if the relay is in a healthy state.
func (r *Relay) IsHealthy() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.State == RelayStateActive && !r.IsExpired()
}

// SetState updates the relay state thread-safely.
func (r *Relay) SetState(state RelayState) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.State = state
}

// IncrementFail increments the failure counter.
func (r *Relay) IncrementFail() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.FailCount++
	return r.FailCount
}

// ResetFail resets the failure counter.
func (r *Relay) ResetFail() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.FailCount = 0
}

// UpdateHealth updates the last health check timestamp.
func (r *Relay) UpdateHealth() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.LastHealth = time.Now()
}

// VKRelayCreateRequest represents the request to create a VK relay.
type VKRelayCreateRequest struct {
	UserID string `json:"user_id"`
	VKHash string `json:"vk_hash,omitempty"`
}

// VKRelayCreateResponse represents the response from creating a VK relay.
type VKRelayCreateResponse struct {
	RelayID   string `json:"relay_id"`
	Endpoint  string `json:"endpoint"`
	VKHash    string `json:"vk_hash"`
	ExpiresIn int    `json:"expires_in"` // seconds
}

// VKRelayManagerConfig holds configuration for the VK Relay Manager.
type VKRelayManagerConfig struct {
	// PanelURL is the base URL of the WDTT panel.
	PanelURL string

	// HTTPClient is the HTTP client to use for panel API calls.
	HTTPClient *http.Client

	// MaxRelaysPerUser is the maximum number of relays per user.
	MaxRelaysPerUser int

	// RenewalBuffer is the time before expiry to start renewal (default 5 minutes).
	RenewalBuffer time.Duration

	// HealthCheckInterval is the interval for health checks (default 30 seconds).
	HealthCheckInterval time.Duration

	// MaxFailures is the number of failures before marking relay as failed.
	MaxFailures int

	// Logger is the logger to use.
	Logger *zerolog.Logger
}

// DefaultVKRelayManagerConfig returns a configuration with sensible defaults.
func DefaultVKRelayManagerConfig() *VKRelayManagerConfig {
	return &VKRelayManagerConfig{
		MaxRelaysPerUser:      4,
		RenewalBuffer:         5 * time.Minute,
		HealthCheckInterval:   30 * time.Second,
		MaxFailures:           3,
		HTTPClient:            &http.Client{Timeout: 10 * time.Second},
	}
}

// VKRelayManager manages the lifecycle of VK relays for multiple users.
type VKRelayManager struct {
	mu           sync.RWMutex
	config       *VKRelayManagerConfig
	logger       zerolog.Logger
	relays       map[string][]*Relay // userID -> list of relays
	userVKHash   map[string]string   // userID -> current vk_hash
	stopCh       chan struct{}
	wg           sync.WaitGroup
	started      bool
}

// NewVKRelayManager creates a new VK Relay Manager.
func NewVKRelayManager(cfg *VKRelayManagerConfig) (*VKRelayManager, error) {
	if cfg == nil {
		cfg = DefaultVKRelayManagerConfig()
	}

	if cfg.PanelURL == "" {
		return nil, errors.New("panel URL is required")
	}

	if cfg.MaxRelaysPerUser <= 0 {
		cfg.MaxRelaysPerUser = 4
	}
	if cfg.RenewalBuffer <= 0 {
		cfg.RenewalBuffer = 5 * time.Minute
	}
	if cfg.HealthCheckInterval <= 0 {
		cfg.HealthCheckInterval = 30 * time.Second
	}
	if cfg.MaxFailures <= 0 {
		cfg.MaxFailures = 3
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: 10 * time.Second}
	}

	logger := zerolog.Nop()
	if cfg.Logger != nil {
		logger = *cfg.Logger
	}

	return &VKRelayManager{
		config:     cfg,
		logger:     logger.With().Str("component", "vkrelay_manager").Logger(),
		relays:     make(map[string][]*Relay),
		userVKHash: make(map[string]string),
		stopCh:     make(chan struct{}),
	}, nil
}

// Start begins the background health check and renewal goroutines.
func (m *VKRelayManager) Start(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.started {
		return errors.New("manager already started")
	}

	m.started = true
	m.stopCh = make(chan struct{})

	m.wg.Add(1)
	go m.healthCheckLoop(ctx)

	m.wg.Add(1)
	go m.renewalLoop(ctx)

	m.logger.Info().Msg("VK Relay Manager started")
	return nil
}

// Stop stops the manager and cleans up all relays.
func (m *VKRelayManager) Stop() error {
	m.mu.Lock()
	if !m.started {
		m.mu.Unlock()
		return nil
	}
	m.started = false
	m.mu.Unlock()

	close(m.stopCh)
	m.wg.Wait()

	m.mu.Lock()
	defer m.mu.Unlock()

	// Close all relays
	for userID, relays := range m.relays {
		for _, relay := range relays {
			relay.SetState(RelayStateExpired)
			m.logger.Info().
				Str("user_id", userID).
				Str("relay_id", relay.ID).
				Msg("relay stopped")
		}
	}
	m.relays = make(map[string][]*Relay)
	m.userVKHash = make(map[string]string)

	m.logger.Info().Msg("VK Relay Manager stopped")
	return nil
}

// CreateRelay creates a new VK relay for the given user.
// If vkHash is provided, it will be used; otherwise the current vk_hash for the user is used.
func (m *VKRelayManager) CreateRelay(ctx context.Context, userID, vkHash string) (*Relay, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if !m.started {
		return nil, errors.New("manager not started")
	}

	// Check max relays per user
	userRelays := m.relays[userID]
	activeCount := 0
	for _, r := range userRelays {
		r.mu.RLock()
		state := r.State
		r.mu.RUnlock()
		if state == RelayStateActive || state == RelayStatePending {
			activeCount++
		}
	}

	if activeCount >= m.config.MaxRelaysPerUser {
		return nil, fmt.Errorf("max relays per user (%d) reached", m.config.MaxRelaysPerUser)
	}

	// Use provided vkHash or fall back to stored one
	if vkHash == "" {
		vkHash = m.userVKHash[userID]
	}

	// Call panel API to create relay
	resp, err := m.callPanelCreateRelay(ctx, userID, vkHash)
	if err != nil {
		m.logger.Error().Err(err).Str("user_id", userID).Msg("failed to create relay via panel API")
		return nil, fmt.Errorf("panel API create relay: %w", err)
	}

	// Create relay object
	now := time.Now()
	relay := &Relay{
		ID:        resp.RelayID,
		UserID:    userID,
		VKHash:    resp.VKHash,
		Endpoint:  resp.Endpoint,
		State:     RelayStatePending,
		CreatedAt: now,
		ExpiresAt: now.Add(time.Duration(resp.ExpiresIn) * time.Second),
	}

	m.relays[userID] = append(m.relays[userID], relay)
	m.userVKHash[userID] = resp.VKHash

	m.logger.Info().
		Str("user_id", userID).
		Str("relay_id", relay.ID).
		Str("vk_hash", resp.VKHash).
		Time("expires_at", relay.ExpiresAt).
		Msg("relay created (pending)")

	// Start health check for this relay
	go m.monitorRelay(ctx, relay)

	return relay, nil
}

// GetRelays returns all relays for a user.
func (m *VKRelayManager) GetRelays(userID string) []*Relay {
	m.mu.RLock()
	defer m.mu.RUnlock()

	relays := m.relays[userID]
	result := make([]*Relay, len(relays))
	copy(result, relays)
	return result
}

// GetActiveRelay returns the first healthy active relay for a user, or nil if none.
func (m *VKRelayManager) GetActiveRelay(userID string) *Relay {
	m.mu.RLock()
	defer m.mu.RUnlock()

	for _, relay := range m.relays[userID] {
		if relay.IsHealthy() {
			return relay
		}
	}
	return nil
}

// GetRelayByID returns a specific relay by ID.
func (m *VKRelayManager) GetRelayByID(userID, relayID string) *Relay {
	m.mu.RLock()
	defer m.mu.RUnlock()

	for _, relay := range m.relays[userID] {
		relay.mu.RLock()
		id := relay.ID
		relay.mu.RUnlock()
		if id == relayID {
			return relay
		}
	}
	return nil
}

// RemoveRelay removes a relay for a user.
func (m *VKRelayManager) RemoveRelay(userID, relayID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()

	relays := m.relays[userID]
	for i, relay := range relays {
		relay.mu.RLock()
		id := relay.ID
		relay.mu.RUnlock()
		if id == relayID {
			relay.SetState(RelayStateExpired)
			m.relays[userID] = append(relays[:i], relays[i+1:]...)
			m.logger.Info().Str("user_id", userID).Str("relay_id", relayID).Msg("relay removed")
			return true
		}
	}
	return false
}

// SetVKHash updates the vk_hash for a user and triggers rotation if changed.
func (m *VKRelayManager) SetVKHash(ctx context.Context, userID, vkHash string) error {
	m.mu.Lock()
	oldHash := m.userVKHash[userID]
	m.userVKHash[userID] = vkHash
	m.mu.Unlock()

	if oldHash != "" && oldHash != vkHash {
		m.logger.Info().
			Str("user_id", userID).
			Str("old_hash", oldHash).
			Str("new_hash", vkHash).
			Msg("vk_hash changed, rotating relays")
		return m.rotateRelays(ctx, userID, vkHash)
	}
	return nil
}

// rotateRelays gracefully rotates relays when vk_hash changes.
func (m *VKRelayManager) rotateRelays(ctx context.Context, userID, newVKHash string) error {
	m.mu.Lock()
	relays := m.relays[userID]
	m.mu.Unlock()

	// Create new relay with new vk_hash first
	newRelay, err := m.CreateRelay(ctx, userID, newVKHash)
	if err != nil {
		m.logger.Error().Err(err).Str("user_id", userID).Msg("failed to create new relay during rotation")
		return err
	}

	// Wait for new relay to become active
	timeout := time.After(30 * time.Second)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-timeout:
			return errors.New("new relay did not become active in time")
		case <-ticker.C:
			if newRelay.IsHealthy() {
				break
			}
		}
	}

	// Mark old relays as expired gracefully
	m.mu.Lock()
	for _, relay := range m.relays[userID] {
		if relay.ID != newRelay.ID {
			relay.mu.RLock()
			state := relay.State
			relay.mu.RUnlock()
			if state == RelayStateActive || state == RelayStatePending {
				relay.SetState(RelayStateExpired)
				m.logger.Info().
					Str("user_id", userID).
					Str("relay_id", relay.ID).
					Msg("old relay expired during rotation")
			}
		}
	}
	m.mu.Unlock()

	return nil
}

// callPanelCreateRelay calls the panel API to create a VK relay.
func (m *VKRelayManager) callPanelCreateRelay(ctx context.Context, userID, vkHash string) (*VKRelayCreateResponse, error) {
	url := m.config.PanelURL + "/panel/api/vk/call/create"

	reqBody := VKRelayCreateRequest{
		UserID: userID,
		VKHash: vkHash,
	}

	jsonBody, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, nil)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Body = &nopCloser{bytes.NewReader(jsonBody)}

	resp, err := m.config.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("do request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("panel API returned status %d", resp.StatusCode)
	}

	var result VKRelayCreateResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	if result.RelayID == "" || result.Endpoint == "" {
		return nil, errors.New("invalid response from panel API: missing relay_id or endpoint")
	}

	return &result, nil
}

// monitorRelay monitors a single relay's health.
func (m *VKRelayManager) monitorRelay(ctx context.Context, relay *Relay) {
	ticker := time.NewTicker(m.config.HealthCheckInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-m.stopCh:
			return
		case <-ticker.C:
			healthy := m.checkRelayHealth(ctx, relay)
			if !healthy {
				failCount := relay.IncrementFail()
				m.logger.Warn().
					Str("user_id", relay.UserID).
					Str("relay_id", relay.ID).
					Int("fail_count", failCount).
					Msg("relay health check failed")

				if failCount >= m.config.MaxFailures {
					relay.SetState(RelayStateFailed)
					m.logger.Error().
						Str("user_id", relay.UserID).
						Str("relay_id", relay.ID).
						Msg("relay marked as failed after max failures")
					m.failoverRelay(relay.UserID, relay.ID)
					return
				}
			} else {
				relay.ResetFail()
				relay.UpdateHealth()
				if relay.State == RelayStatePending {
					relay.SetState(RelayStateActive)
					m.logger.Info().
						Str("user_id", relay.UserID).
						Str("relay_id", relay.ID).
						Msg("relay became active")
				}
			}

			// Check expiry
			if relay.IsExpired() {
				relay.SetState(RelayStateExpired)
				m.logger.Info().
					Str("user_id", relay.UserID).
					Str("relay_id", relay.ID).
					Msg("relay expired")
				return
			}
		}
	}
}

// checkRelayHealth performs a health check on a relay.
func (m *VKRelayManager) checkRelayHealth(ctx context.Context, relay *Relay) bool {
	// Create a simple HTTP request to the relay endpoint to verify it's alive
	// This is a placeholder - implement actual health check based on your relay protocol
	req, err := http.NewRequestWithContext(ctx, "GET", relay.Endpoint+"/health", nil)
	if err != nil {
		return false
	}

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()

	return resp.StatusCode == http.StatusOK
}

// failoverRelay attempts to failover to the next available relay for a user.
func (m *VKRelayManager) failoverRelay(userID, failedRelayID string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	relays := m.relays[userID]
	for _, relay := range relays {
		relay.mu.RLock()
		id := relay.ID
		state := relay.State
		relay.mu.RUnlock()

		if id != failedRelayID && (state == RelayStateActive || state == RelayStatePending) {
			m.logger.Info().
				Str("user_id", userID).
				Str("failed_relay", failedRelayID).
				Str("failover_relay", id).
				Msg("failover to next relay")
			return
		}
	}

	// No healthy relay available, try to create a new one
	m.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	m.mu.Lock()

	_, err := m.CreateRelay(ctx, userID, m.userVKHash[userID])
	if err != nil {
		m.logger.Error().Err(err).Str("user_id", userID).Msg("failed to create replacement relay during failover")
	}
}

// healthCheckLoop runs periodic health checks on all relays.
func (m *VKRelayManager) healthCheckLoop(ctx context.Context) {
	defer m.wg.Done()

	ticker := time.NewTicker(m.config.HealthCheckInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-m.stopCh:
			return
		case <-ticker.C:
			m.mu.RLock()
			for userID, relays := range m.relays {
				for _, relay := range relays {
					if relay.IsExpired() {
						relay.SetState(RelayStateExpired)
						continue
					}
					if relay.State == RelayStateActive || relay.State == RelayStatePending {
						healthy := m.checkRelayHealth(ctx, relay)
						if !healthy {
							failCount := relay.IncrementFail()
							if failCount >= m.config.MaxFailures {
								relay.SetState(RelayStateFailed)
								m.failoverRelay(userID, relay.ID)
							}
						} else {
							relay.ResetFail()
							relay.UpdateHealth()
							if relay.State == RelayStatePending {
								relay.SetState(RelayStateActive)
							}
						}
					}

					// Check expiry
					if relay.IsExpired() {
						relay.SetState(RelayStateExpired)
					}
				}
			}
			m.mu.RUnlock()
		}
	}
}

// renewalLoop runs periodic checks for relays that need renewal.
func (m *VKRelayManager) renewalLoop(ctx context.Context) {
	defer m.wg.Done()

	ticker := time.NewTicker(1 * time.Minute)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-m.stopCh:
			return
		case <-ticker.C:
			m.mu.RLock()
			for userID, relays := range m.relays {
				for _, relay := range relays {
					relay.mu.RLock()
					state := relay.State
					expiresAt := relay.ExpiresAt
					vkHash := relay.VKHash
					relay.mu.RUnlock()

					if state == RelayStateActive {
						timeUntilExpiry := time.Until(expiresAt)
						if timeUntilExpiry <= m.config.RenewalBuffer && timeUntilExpiry > 0 {
							m.logger.Info().
								Str("user_id", userID).
								Str("relay_id", relay.ID).
								Dur("time_until_expiry", timeUntilExpiry).
								Msg("renewing relay before expiry")
							m.mu.RUnlock()
							m.renewRelay(ctx, userID, relay, vkHash)
							m.mu.RLock()
						} else if timeUntilExpiry <= 0 {
							relay.SetState(RelayStateExpired)
						}
					}
				}
			}
			m.mu.RUnlock()
		}
	}
}

// renewRelay renews a relay by creating a new one with the same vk_hash.
func (m *VKRelayManager) renewRelay(ctx context.Context, userID string, oldRelay *Relay, vkHash string) {
	newRelay, err := m.CreateRelay(ctx, userID, vkHash)
	if err != nil {
		m.logger.Error().Err(err).
			Str("user_id", userID).
			Str("old_relay_id", oldRelay.ID).
			Msg("failed to renew relay")
		return
	}

	// Wait for new relay to become active
	timeout := time.After(30 * time.Second)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-timeout:
			m.logger.Error().
				Str("user_id", userID).
				Str("new_relay_id", newRelay.ID).
				Msg("new relay did not become active during renewal")
			return
		case <-ticker.C:
			if newRelay.IsHealthy() {
				break
			}
		}
	}

	// Mark old relay as expired
	oldRelay.SetState(RelayStateExpired)
	m.logger.Info().
		Str("user_id", userID).
		Str("old_relay_id", oldRelay.ID).
		Str("new_relay_id", newRelay.ID).
		Msg("relay renewed successfully")
}

// nopCloser wraps an io.Reader to implement io.ReadCloser with a no-op Close.
type nopCloser struct {
	io.Reader
}

func (nopCloser) Close() error { return nil }