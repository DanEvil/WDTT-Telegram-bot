package wdtt

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/pion/webrtc/v3"
)

// VKRelayConfig holds configuration for the VK Relay WebRTC connection.
type VKRelayConfig struct {
	// PanelAPIBase is the base URL of the WDTT Panel API (e.g., "https://panel.example.com").
	PanelAPIBase string
	// HTTPClient is the HTTP client to use for API calls (nil = default).
	HTTPClient *http.Client
	// STUNServers is a list of STUN server URLs for ICE candidate gathering.
	STUNServers []string
	// TURNConfig holds TURN server configuration (optional).
	TURNConfig *TURNConfig
	// DTLSFingerprint is the expected DTLS certificate fingerprint for verification (SHA-256 hex).
	// If empty, fingerprint verification is skipped.
	DTLSFingerprint string
	// DialTimeout is the timeout for establishing the WebRTC connection.
	DialTimeout time.Duration
	// OnStateChange is called when the connection state changes.
	OnStateChange func(state ConnectionState)
	// OnDataChannelOpen is called when a data channel is opened.
	OnDataChannelOpen func(channel *webrtc.DataChannel)
	// OnDataChannelMessage is called when a message is received on a data channel.
	OnDataChannelMessage func(channel *webrtc.DataChannel, msg webrtc.DataChannelMessage)
	// OnICEConnectionStateChange is called when ICE connection state changes.
	OnICEConnectionStateChange func(state webrtc.ICEConnectionState)
}

// TURNConfig holds TURN server configuration.
type TURNConfig struct {
	URLs       []string
	Username   string
	Credential string
}

// ConnectionState represents the WebRTC peer connection state.
type ConnectionState string

const (
	StateNew           ConnectionState = "new"
	StateConnecting    ConnectionState = "connecting"
	StateConnected     ConnectionState = "connected"
	StateDisconnected  ConnectionState = "disconnected"
	StateFailed        ConnectionState = "failed"
	StateClosed        ConnectionState = "closed"
)

// ICECandidate represents an ICE candidate for signaling.
type ICECandidate struct {
	Candidate        string `json:"candidate"`
	SDPMid           string `json:"sdpMid"`
	SDPMLineIndex    uint16 `json:"sdpMLineIndex"`
	UsernameFragment string `json:"usernameFragment,omitempty"`
}

// SessionDescription represents a WebRTC session description for signaling.
type SessionDescription struct {
	Type webrtc.SDPType `json:"type"`
	SDP  string         `json:"sdp"`
}

// CreateVKCallRequest represents the request to create a VK call via Panel API.
type CreateVKCallRequest struct {
	UserID      string `json:"user_id"`
	OfferSDP    string `json:"offer_sdp"`
	OfferType   string `json:"offer_type"`
	Fingerprint string `json:"fingerprint,omitempty"` // DTLS fingerprint
}

// CreateVKCallResponse represents the response from creating a VK call.
type CreateVKCallResponse struct {
	CallID     string        `json:"call_id"`
	AnswerSDP  string        `json:"answer_sdp"`
	AnswerType string        `json:"answer_type"`
	ICEConfigs []ICECandidate `json:"ice_configs,omitempty"`
	ExpiresAt  int64         `json:"expires_at,omitempty"`
}

// VKCallStatus represents the status of a VK call from the Panel API.
type VKCallStatus struct {
	CallID    string `json:"call_id"`
	Status    string `json:"status"` // "pending", "active", "ended", "failed"
	Error     string `json:"error,omitempty"`
	UpdatedAt int64  `json:"updated_at"`
}

// VKRelay manages a WebRTC peer connection for VK Relay.
type VKRelay struct {
	mu               sync.RWMutex
	config           VKRelayConfig
	peerConnection   *webrtc.PeerConnection
	dataChannels     map[string]*webrtc.DataChannel
	state            ConnectionState
	callID           string
	ctx              context.Context
	cancel           context.CancelFunc
	wg               sync.WaitGroup

	httpClient       *http.Client
	iceGathered      bool
	iceGatherDone    chan struct{}
	localDescription *webrtc.SessionDescription
}

// NewVKRelay creates a new VKRelay instance with the given configuration.
func NewVKRelay(config VKRelayConfig) (*VKRelay, error) {
	if config.PanelAPIBase == "" {
		return nil, errors.New("panel_api_base is required")
	}
	if config.DialTimeout <= 0 {
		config.DialTimeout = 30 * time.Second
	}
	if config.STUNServers == nil || len(config.STUNServers) == 0 {
		config.STUNServers = []string{
			"stun:stun.l.google.com:19302",
			"stun:stun1.l.google.com:19302",
			"stun:stun2.l.google.com:19302",
		}
	}
	if config.HTTPClient == nil {
		config.HTTPClient = &http.Client{Timeout: 10 * time.Second}
	}

	ctx, cancel := context.WithCancel(context.Background())

	r := &VKRelay{
		config:       config,
		dataChannels: make(map[string]*webrtc.DataChannel),
		state:        StateNew,
		ctx:          ctx,
		cancel:       cancel,
		httpClient:   config.HTTPClient,
		iceGatherDone: make(chan struct{}),
	}

	return r, nil
}

// createPeerConnection creates and configures a new WebRTC peer connection with ICE/STUN.
func (r *VKRelay) createPeerConnection() (*webrtc.PeerConnection, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.peerConnection != nil {
		return r.peerConnection, nil
	}

	// Build ICE servers configuration
	iceServers := make([]webrtc.ICEServer, 0, len(r.config.STUNServers)+1)

	// Add STUN servers
	for _, stun := range r.config.STUNServers {
		iceServers = append(iceServers, webrtc.ICEServer{
			URLs: []string{stun},
		})
	}

	// Add TURN server if configured
	if r.config.TURNConfig != nil && len(r.config.TURNConfig.URLs) > 0 {
		iceServers = append(iceServers, webrtc.ICEServer{
			URLs:       r.config.TURNConfig.URLs,
			Username:   r.config.TURNConfig.Username,
			Credential: r.config.TURNConfig.Credential,
		})
	}

	// Configure peer connection
	setting := webrtc.SettingEngine{}
	setting.SetICETimeouts(5*time.Second, 25*time.Second, 10*time.Second)
	setting.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4, webrtc.NetworkTypeUDP6})

	// Create peer connection with custom settings
	peerConnection, err := webrtc.NewPeerConnection(webrtc.Configuration{
		ICEServers: iceServers,
	}, setting)
	if err != nil {
		return nil, fmt.Errorf("failed to create peer connection: %w", err)
	}

	r.peerConnection = peerConnection

	// Set up connection state handlers
	peerConnection.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		r.handleConnectionStateChange(state)
	})

	peerConnection.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		r.handleICEConnectionStateChange(state)
	})

	peerConnection.OnICECandidate(func(candidate *webrtc.ICECandidate) {
		r.handleICECandidate(candidate)
	})

	peerConnection.OnDataChannel(func(channel *webrtc.DataChannel) {
		r.handleDataChannel(channel)
	})

	peerConnection.OnTrack(func(track *webrtc.TrackRemote, receiver *webrtc.RTPReceiver) {
		// Handle incoming tracks if needed
	})

	return peerConnection, nil
}

// handleConnectionStateChange handles peer connection state changes.
func (r *VKRelay) handleConnectionStateChange(state webrtc.PeerConnectionState) {
	var newState ConnectionState

	switch state {
	case webrtc.PeerConnectionStateNew:
		newState = StateNew
	case webrtc.PeerConnectionStateConnecting:
		newState = StateConnecting
	case webrtc.PeerConnectionStateConnected:
		newState = StateConnected
	case webrtc.PeerConnectionStateDisconnected:
		newState = StateDisconnected
	case webrtc.PeerConnectionStateFailed:
		newState = StateFailed
	case webrtc.PeerConnectionStateClosed:
		newState = StateClosed
	default:
		newState = StateNew
	}

	r.mu.Lock()
	r.state = newState
	r.mu.Unlock()

	if r.config.OnStateChange != nil {
		r.config.OnStateChange(newState)
	}
}

// handleICEConnectionStateChange handles ICE connection state changes.
func (r *VKRelay) handleICEConnectionStateChange(state webrtc.ICEConnectionState) {
	if r.config.OnICEConnectionStateChange != nil {
		r.config.OnICEConnectionStateChange(state)
	}
}

// handleICECandidate handles new ICE candidates.
func (r *VKRelay) handleICECandidate(candidate *webrtc.ICECandidate) {
	if candidate == nil {
		// ICE gathering complete
		r.mu.Lock()
		r.iceGathered = true
		select {
		case <-r.iceGatherDone:
		default:
			close(r.iceGatherDone)
		}
		r.mu.Unlock()
		return
	}

	// Candidate can be sent via signaling
	// The caller can access candidates via GetICECandidates()
}

// handleDataChannel handles incoming data channels.
func (r *VKRelay) handleDataChannel(channel *webrtc.DataChannel) {
	r.mu.Lock()
	r.dataChannels[channel.Label()] = channel
	r.mu.Unlock()

	channel.OnOpen(func() {
		if r.config.OnDataChannelOpen != nil {
			r.config.OnDataChannelOpen(channel)
		}
	})

	channel.OnMessage(func(msg webrtc.DataChannelMessage) {
		if r.config.OnDataChannelMessage != nil {
			r.config.OnDataChannelMessage(channel, msg)
		}
	})

	channel.OnClose(func() {
		r.mu.Lock()
		delete(r.dataChannels, channel.Label())
		r.mu.Unlock()
	})
}

// verifyDTLSFingerprint verifies the DTLS certificate fingerprint.
func (r *VKRelay) verifyDTLSFingerprint() error {
	if r.config.DTLSFingerprint == "" {
		return nil // No fingerprint configured, skip verification
	}

	r.mu.RLock()
	pc := r.peerConnection
	r.mu.RUnlock()

	if pc == nil {
		return errors.New("peer connection not initialized")
	}

	// Get the DTLS transport
	transports := pc.GetTransports()
	if len(transports) == 0 {
		return errors.New("no DTLS transports available")
	}

	dtlsTransport := transports[0].DTLSTransport()
	if dtlsTransport == nil {
		return errors.New("DTLS transport not available")
	}

	// Get the remote certificate chain
	certs := dtlsTransport.GetRemoteCertificates()
	if len(certs) == 0 {
		return errors.New("no remote certificates available")
	}

	// Verify the first certificate's fingerprint
	remoteCert := certs[0]
	fingerprint := sha256.Sum256(remoteCert.Raw)
	fingerprintHex := hex.EncodeToString(fingerprint[:])

	expected := r.config.DTLSFingerprint
	if fingerprintHex != expected {
		return fmt.Errorf("DTLS fingerprint mismatch: expected %s, got %s", expected, fingerprintHex)
	}

	return nil
}

// CreateOffer creates a local offer and initiates the WebRTC connection.
func (r *VKRelay) CreateOffer(ctx context.Context) (*webrtc.SessionDescription, error) {
	pc, err := r.createPeerConnection()
	if err != nil {
		return nil, err
	}

	// Create a data channel for DTLS traffic
	dataChannel, err := pc.CreateDataChannel("dtls-traffic", &webrtc.DataChannelInit{
		Ordered:        boolPtr(true),
		MaxRetransmits: nil, // Reliable
		Protocol:       "dtls",
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create data channel: %w", err)
	}

	r.mu.Lock()
	r.dataChannels[dataChannel.Label()] = dataChannel
	r.mu.Unlock()

	// Create offer
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create offer: %w", err)
	}

	// Set local description
	if err := pc.SetLocalDescription(offer); err != nil {
		return nil, fmt.Errorf("failed to set local description: %w", err)
	}

	r.mu.Lock()
	r.localDescription = &offer
	r.mu.Unlock()

	// Wait for ICE gathering to complete
	select {
	case <-r.iceGatherDone:
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(r.config.DialTimeout):
		// Timeout but continue anyway
	}

	// Verify DTLS fingerprint if configured
	if err := r.verifyDTLSFingerprint(); err != nil {
		return nil, fmt.Errorf("DTLS fingerprint verification failed: %w", err)
	}

	// Get the complete offer with gathered ICE candidates
	r.mu.RLock()
	completeOffer := r.peerConnection.LocalDescription()
	r.mu.RUnlock()

	return completeOffer, nil
}

// SetRemoteDescription sets the remote answer from the signaling server.
func (r *VKRelay) SetRemoteDescription(answer webrtc.SessionDescription) error {
	r.mu.RLock()
	pc := r.peerConnection
	r.mu.RUnlock()

	if pc == nil {
		return errors.New("peer connection not initialized")
	}

	return pc.SetRemoteDescription(answer)
}

// AddICECandidate adds a remote ICE candidate.
func (r *VKRelay) AddICECandidate(candidate webrtc.ICECandidateInit) error {
	r.mu.RLock()
	pc := r.peerConnection
	r.mu.RUnlock()

	if pc == nil {
		return errors.New("peer connection not initialized")
	}

	return pc.AddICECandidate(candidate)
}

// GetICECandidates returns all gathered local ICE candidates.
func (r *VKRelay) GetICECandidates() []ICECandidate {
	r.mu.RLock()
	pc := r.peerConnection
	r.mu.RUnlock()

	if pc == nil {
		return nil
	}

	// Note: pion/webrtc doesn't expose a direct way to get all gathered candidates
	// They are included in the local description SDP
	return nil
}

// GetConnectionState returns the current connection state.
func (r *VKRelay) GetConnectionState() ConnectionState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.state
}

// GetDataChannel returns a data channel by label.
func (r *VKRelay) GetDataChannel(label string) *webrtc.DataChannel {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.dataChannels[label]
}

// CreateDataChannel creates a new data channel.
func (r *VKRelay) CreateDataChannel(label string, init *webrtc.DataChannelInit) (*webrtc.DataChannel, error) {
	r.mu.RLock()
	pc := r.peerConnection
	r.mu.RUnlock()

	if pc == nil {
		return nil, errors.New("peer connection not initialized")
	}

	channel, err := pc.CreateDataChannel(label, init)
	if err != nil {
		return nil, err
	}

	r.mu.Lock()
	r.dataChannels[label] = channel
	r.mu.Unlock()

	return channel, nil
}

// create_vk_call creates a VK call via the WDTT Panel API.
func (r *VKRelay) create_vk_call(ctx context.Context, userID string, offer *webrtc.SessionDescription) (*CreateVKCallResponse, error) {
	if r.config.PanelAPIBase == "" {
		return nil, errors.New("panel_api_base not configured")
	}

	// Compute DTLS fingerprint if configured
	var fingerprint string
	if r.config.DTLSFingerprint != "" {
		fingerprint = r.config.DTLSFingerprint
	}

	reqBody := CreateVKCallRequest{
		UserID:     userID,
		OfferSDP:   offer.SDP,
		OfferType:  offer.Type.String(),
		Fingerprint: fingerprint,
	}

	jsonBody, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	url := r.config.PanelAPIBase + "/panel/api/vk/calls"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Body = &nopCloser{bytes.NewReader(jsonBody)}

	resp, err := r.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("API returned status %d", resp.StatusCode)
	}

	var result CreateVKCallResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	r.mu.Lock()
	r.callID = result.CallID
	r.mu.Unlock()

	return &result, nil
}

// get_vk_status retrieves the VK call status from the WDTT Panel API.
func (r *VKRelay) get_vk_status(ctx context.Context, callID string) (*VKCallStatus, error) {
	if r.config.PanelAPIBase == "" {
		return nil, errors.New("panel_api_base not configured")
	}

	url := fmt.Sprintf("%s/panel/api/vk/calls/%s", r.config.PanelAPIBase, callID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	resp, err := r.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("API returned status %d", resp.StatusCode)
	}

	var status VKCallStatus
	if err := json.NewDecoder(resp.Body).Decode(&status); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	return &status, nil
}

// Connect initiates the full WebRTC connection flow via Panel API signaling.
func (r *VKRelay) Connect(ctx context.Context, userID string) error {
	// Create offer
	offer, err := r.CreateOffer(ctx)
	if err != nil {
		return fmt.Errorf("failed to create offer: %w", err)
	}

	// Signal offer to Panel API and get answer
	resp, err := r.create_vk_call(ctx, userID, offer)
	if err != nil {
		return fmt.Errorf("failed to create VK call: %w", err)
	}

	// Parse answer
	answer := webrtc.SessionDescription{
		Type: webrtc.SDPType(resp.AnswerType),
		SDP:  resp.AnswerSDP,
	}

	// Set remote description
	if err := r.SetRemoteDescription(answer); err != nil {
		return fmt.Errorf("failed to set remote description: %w", err)
	}

	// Add ICE candidates from response
	for _, ice := range resp.ICEConfigs {
		candidate := webrtc.ICECandidateInit{
			Candidate:        ice.Candidate,
			SDPMid:           &ice.SDPMid,
			SDPMLineIndex:    &ice.SDPMLineIndex,
			UsernameFragment: ice.UsernameFragment,
		}
		if err := r.AddICECandidate(candidate); err != nil {
			// Log but continue
			fmt.Printf("Failed to add ICE candidate: %v\n", err)
		}
	}

	// Wait for connection to be established
	return r.waitForConnected(ctx)
}

// waitForConnected waits until the connection is established or fails.
func (r *VKRelay) waitForConnected(ctx context.Context) error {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			state := r.GetConnectionState()
			switch state {
			case StateConnected:
				return nil
			case StateFailed, StateClosed:
				return fmt.Errorf("connection failed with state: %s", state)
			}
		}
	}
}

// SendDTLSData sends DTLS traffic over the data channel.
func (r *VKRelay) SendDTLSData(data []byte) error {
	channel := r.GetDataChannel("dtls-traffic")
	if channel == nil {
		return errors.New("DTLS data channel not available")
	}

	if channel.ReadyState() != webrtc.DataChannelStateOpen {
		return errors.New("data channel not open")
	}

	return channel.Send(data)
}

// Close closes the WebRTC connection and cleans up resources.
func (r *VKRelay) Close() error {
	r.cancel()

	r.mu.Lock()
	pc := r.peerConnection
	r.peerConnection = nil

	// Close all data channels
	for _, ch := range r.dataChannels {
		ch.Close()
	}
	r.dataChannels = make(map[string]*webrtc.DataChannel)
	r.mu.Unlock()

	if pc != nil {
		return pc.Close()
	}

	r.wg.Wait()
	return nil
}

// boolPtr returns a pointer to a bool value.
func boolPtr(b bool) *bool {
	return &b
}

// nopCloser wraps a Reader to add a no-op Close method.
type nopCloser struct {
	*bytes.Reader
}

func (nopCloser) Close() error { return nil }