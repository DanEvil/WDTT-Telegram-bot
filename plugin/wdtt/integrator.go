package wdtt

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/pion/dtls/v2"
	"github.com/pion/webrtc/v3"
)

// VKHashServiceConfig holds configuration for the lazy VK hash service
type VKHashServiceConfig struct {
	BaseURL    string        `json:"base_url"`     // e.g., "http://localhost:8083"
	HTTPClient *http.Client  `json:"-"`
	Timeout    time.Duration `json:"timeout"`
}

// VKHashInitResponse represents the response from the VK hash service
type VKHashInitResponse struct {
	Success     bool   `json:"success"`
	VKHash      string `json:"vk_hash"`
	Initialized bool   `json:"initialized"`
	Error       string `json:"error"`
}

// IntegratorConfig holds configuration for the Integrator.
type IntegratorConfig struct {
	// VKHashManagerConfig configures the VK hash manager.
	VKHashManagerConfig VKHashConfig
	// VKHashServiceConfig configures the lazy VK hash initialization service.
	VKHashServiceConfig *VKHashServiceConfig
	// VKRelayManagerConfig configures the VK relay manager.
	VKRelayManagerConfig *VKRelayManagerConfig
	// DTLSConfig is the DTLS configuration for WebRTC connections.
	DTLSConfig *dtls.Config
	// DialTimeout is the timeout for establishing connections.
	DialTimeout time.Duration
	// WireGuardConfig is the WireGuard configuration for fallback.
	WireGuardConfig *WireGuardConfig
	// PoolConfig configures the connection pool.
	PoolConfig PoolConfig
}

// Integrator wires together VKHashManager, VKRelayManager, DTLSOverWebRTCTransport, and Pool.
type Integrator struct {
	mu            sync.RWMutex
	config        IntegratorConfig
	vkHashManager *VKHashManager
	vkHashService *VKHashServiceConfig
	relayManager  *VKRelayManager
	pool          *Pool
	started       bool
	stopCh        chan struct{}
	wg            sync.WaitGroup
}

// NewIntegrator creates a new Integrator with the given configuration.
func NewIntegrator(config IntegratorConfig) (*Integrator, error) {
	if config.DialTimeout <= 0 {
		config.DialTimeout = 10 * time.Second
	}

	// Create VKHashManager
	vkHashManager, err := NewVKHashManager(config.VKHashManagerConfig)
	if err != nil {
		return nil, err
	}

	// Create VKRelayManager
	relayManager, err := NewVKRelayManager(config.VKRelayManagerConfig)
	if err != nil {
		vkHashManager.Stop()
		return nil, err
	}

	// Set up hash change callback to trigger relay rotation
	if config.VKHashManagerConfig.OnHashChange == nil {
		vkHashManagerConfig := config.VKHashManagerConfig
		vkHashManagerConfig.OnHashChange = func(userID, oldHash, newHash string) {
			ctx := context.Background()
			if err := relayManager.SetVKHash(ctx, userID, newHash); err != nil {
				// Log error but don't panic
			}
		}
		// Re-create VKHashManager with the callback
		vkHashManager.Stop()
		vkHashManager, err = NewVKHashManager(vkHashManagerConfig)
		if err != nil {
			relayManager.Stop()
			return nil, err
		}
	}

	// Create pool with a placeholder dialer (will be replaced per-user via GetDialer)
	poolConfig := config.PoolConfig
	if poolConfig.Dialer == nil {
		poolConfig.Dialer = func(ctx context.Context, creds Credentials) (net.Conn, error) {
			return nil, errors.New("no dialer configured for user; call GetDialer first")
		}
	}
	pool := NewPool(poolConfig)

	return &Integrator{
		config:         config,
		vkHashManager:  vkHashManager,
		vkHashService:  config.VKHashServiceConfig,
		relayManager:   relayManager,
		pool:           pool,
		stopCh:         make(chan struct{}),
	}, nil
}

// Start starts the integrator and all its components.
func (i *Integrator) Start(ctx context.Context) error {
	i.mu.Lock()
	defer i.mu.Unlock()

	if i.started {
		return errors.New("integrator already started")
	}

	// Start relay manager
	if err := i.relayManager.Start(ctx); err != nil {
		return err
	}

	i.started = true
	i.stopCh = make(chan struct{})

	i.wg.Add(1)
	go i.backgroundLoop(ctx)

	return nil
}

// Stop stops the integrator and all its components.
func (i *Integrator) Stop() error {
	i.mu.Lock()
	if !i.started {
		i.mu.Unlock()
		return nil
	}
	i.started = false
	close(i.stopCh)
	i.mu.Unlock()

	i.wg.Wait()

	i.pool.Close()
	i.relayManager.Stop()
	i.vkHashManager.Stop()

	return nil
}

// ensureVKHash lazily initializes VK hash for a user on first connection.
// It calls the external VK hash service to create a VK call if not already initialized.
func (i *Integrator) ensureVKHash(ctx context.Context, userID string) error {
	if i.vkHashService == nil || i.vkHashService.BaseURL == "" {
		// No lazy init service configured, skip
		return nil
	}

	// Check if already initialized in VKHashManager cache
	if hash, _ := i.vkHashManager.GetVKHash(userID); hash != "" {
		return nil
	}

	// Prepare HTTP client
	client := i.vkHashService.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: i.vkHashService.Timeout}
		if client.Timeout == 0 {
			client.Timeout = 30 * time.Second
		}
	}

	// Call the VK hash service
	url := i.vkHashService.BaseURL + "/vk-hash/ensure"
	reqBody := map[string]string{"wdtt_password": userID}
	body, _ := json.Marshal(reqBody)

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return errors.New("vk hash service returned status: " + resp.Status)
	}

	var result VKHashInitResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return err
	}

	if !result.Success {
		return errors.New("vk hash service error: " + result.Error)
	}

	// Update VKHashManager cache with the new hash
	if result.VKHash != "" {
		i.vkHashManager.SetManualHash(userID, result.VKHash)
	}

	return nil
}

// GetDialer returns a dialer function for the given user ID.
// The dialer gets the vk_hash from VKHashManager, active relay from VKRelayManager,
// creates a DTLSOverWebRTCTransport with the peer connection, and returns a DTLSWebRTCDialer.
// Falls back to WireGuard if no active relay.
func (i *Integrator) GetDialer(userID string) func(ctx context.Context, creds Credentials) (net.Conn, error) {
	return func(ctx context.Context, creds Credentials) (net.Conn, error) {
		// Ensure VK hash is initialized (lazy initialization on first connection)
		if err := i.ensureVKHash(ctx, userID); err != nil {
			return nil, errors.New("failed to ensure VK hash: " + err.Error())
		}

		// Get vk_hash from VKHashManager
		vkHash := i.vkHashManager.GetHash(userID)
		if vkHash == "" {
			return nil, errors.New("no vk_hash available for user")
		}

		// Get active relay from VKRelayManager
		relay := i.relayManager.GetActiveRelay(userID)
		if relay == nil {
			// No active relay, try to create one
			ctxCreate, cancel := context.WithTimeout(ctx, i.config.DialTimeout)
			defer cancel()

			var err error
			relay, err = i.relayManager.CreateRelay(ctxCreate, userID, vkHash)
			if err != nil {
				// Fallback to WireGuard if configured
				if i.config.WireGuardConfig != nil {
					return i.dialWireGuard(ctx, creds)
				}
				return nil, errors.New("no active relay and failed to create new one: " + err.Error())
			}

			// Wait for relay to become active
			timeout := time.After(30 * time.Second)
			ticker := time.NewTicker(500 * time.Millisecond)
			defer ticker.Stop()

			for {
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-timeout:
					if i.config.WireGuardConfig != nil {
						return i.dialWireGuard(ctx, creds)
					}
					return nil, errors.New("relay did not become active in time")
				case <-ticker.C:
					if relay.IsHealthy() {
						break
					}
				}
			}
		}

		// Create VKRelay with the relay's endpoint and vk_hash
		vkRelayConfig := VKRelayConfig{
			PanelAPIBase:    i.config.VKRelayManagerConfig.PanelURL,
			STUNServers:     []string{"stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302", "stun:stun2.l.google.com:19302"},
			DTLSFingerprint: "", // Could be derived from DTLSConfig if needed
			DialTimeout:     i.config.DialTimeout,
		}

		vkRelay, err := NewVKRelay(vkRelayConfig)
		if err != nil {
			if i.config.WireGuardConfig != nil {
				return i.dialWireGuard(ctx, creds)
			}
			return nil, err
		}

		// Connect to the relay endpoint using the VK hash
		ctxConnect, cancel := context.WithTimeout(ctx, i.config.DialTimeout)
		defer cancel()

		if err := vkRelay.Connect(ctxConnect, userID); err != nil {
			vkRelay.Close()
			if i.config.WireGuardConfig != nil {
				return i.dialWireGuard(ctx, creds)
			}
			return nil, err
		}

		// Get the peer connection from the VKRelay
		peerConn := vkRelay.peerConnection
		if peerConn == nil {
			vkRelay.Close()
			if i.config.WireGuardConfig != nil {
				return i.dialWireGuard(ctx, creds)
			}
			return nil, errors.New("peer connection not established")
		}

		// Create DTLSWebRTCDialer for the pool
		dialer := NewDTLSWebRTCDialer(peerConn, i.config.DTLSConfig, i.config.DialTimeout)

		// Return a dialer that uses the DTLSWebRTCDialer
		return dialer.Dial(ctx, creds)
	}
}

// dialWireGuard establishes a connection using WireGuard as fallback.
func (i *Integrator) dialWireGuard(ctx context.Context, creds Credentials) (net.Conn, error) {
	if i.config.WireGuardConfig == nil {
		return nil, errors.New("wireguard fallback not configured")
	}

	wgTransport, err := NewWireGuardTransport(i.config.WireGuardConfig, i.config.DialTimeout)
	if err != nil {
		return nil, err
	}

	// For WireGuard, we don't have a traditional "dial" to an address.
	// The tunnel is already established. Return a connection through the tunnel.
	// We use a dummy address since WireGuard operates at the network layer.
	return wgTransport.Dial(ctx, "wireguard-tunnel")
}

// backgroundLoop runs background maintenance tasks.
func (i *Integrator) backgroundLoop(ctx context.Context) {
	defer i.wg.Done()

	ticker := time.NewTicker(1 * time.Minute)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-i.stopCh:
			return
		case <-ticker.C:
			i.maintenance()
		}
	}
}

// maintenance performs periodic maintenance tasks.
func (i *Integrator) maintenance() {
	i.mu.RLock()
	started := i.started
	i.mu.RUnlock()

	if !started {
		return
	}

	// Could add stats collection, health checks, etc.
	_ = i.vkHashManager.Stats()
}

// GetPool returns the connection pool for direct access if needed.
func (i *Integrator) GetPool() *Pool {
	return i.pool
}

// GetVKHashManager returns the VK hash manager for direct access if needed.
func (i *Integrator) GetVKHashManager() *VKHashManager {
	return i.vkHashManager
}

// GetRelayManager returns the VK relay manager for direct access if needed.
func (i *Integrator) GetRelayManager() *VKRelayManager {
	return i.relayManager
}

// SetVKHash manually sets the VK hash for a user and triggers relay rotation.
func (i *Integrator) SetVKHash(ctx context.Context, userID, vkHash string) error {
	return i.relayManager.SetVKHash(ctx, userID, vkHash)
}

// CreateRelay creates a new relay for a user.
func (i *Integrator) CreateRelay(ctx context.Context, userID, vkHash string) (*Relay, error) {
	return i.relayManager.CreateRelay(ctx, userID, vkHash)
}

// GetActiveRelay returns the active relay for a user.
func (i *Integrator) GetActiveRelay(userID string) *Relay {
	return i.relayManager.GetActiveRelay(userID)
}