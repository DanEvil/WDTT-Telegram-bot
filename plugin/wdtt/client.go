package wdtt

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"

	"github.com/pion/dtls/v2"
	"github.com/rs/zerolog"
	"github.com/xtaci/wireguard-go/conn"
	"github.com/xtaci/wireguard-go/device"
	"github.com/xtaci/wireguard-go/tun"
	"golang.org/x/crypto/chacha20poly1305"
)

// ClientConfig holds the configuration for the WDTT client.
type ClientConfig struct {
	// Transport is the transport type to use (dtls or wireguard).
	Transport TransportType `json:"transport" validate:"oneof=dtls wireguard"`

	// PanelURL is the URL of the WDTT panel for handshake.
	PanelURL string `json:"panel_url" validate:"required,url"`

	// PanelUsername is the username for panel authentication.
	PanelUsername string `json:"panel_username" validate:"required"`

	// PanelPassword is the password for panel authentication.
	PanelPassword string `json:"panel_password" validate:"required"`

	// VerifySSL controls whether to verify the panel's TLS certificate.
	VerifySSL bool `json:"verify_ssl"`

	// DTLSConfig holds DTLS-specific configuration.
	DTLSConfig *DTLSConfig `json:"dtls_config,omitempty"`

	// WireGuardConfig holds WireGuard-specific configuration.
	WireGuardConfig *WireGuardConfig `json:"wireguard_config,omitempty"`

	// DialTimeout is the timeout for establishing connections.
	DialTimeout time.Duration `json:"dial_timeout"`

	// HandshakeTimeout is the timeout for the WDTT panel handshake.
	HandshakeTimeout time.Duration `json:"handshake_timeout"`

	// Logger is the logger to use. If nil, a default logger is used.
	Logger *zerolog.Logger `json:"-"`
}

// DTLSConfig holds DTLS-specific configuration.
type DTLSConfig struct {
	// CipherSuites is the list of cipher suites to use.
	CipherSuites []uint16 `json:"cipher_suites"`

	// ServerName is the SNI server name to use.
	ServerName string `json:"server_name"`

	// InsecureSkipVerify skips certificate verification (for testing).
	InsecureSkipVerify bool `json:"insecure_skip_verify"`

	// RootCAs is the set of root CAs to trust.
	RootCAs *x509.CertPool `json:"-"`

	// ClientCertificates are the client certificates to present.
	ClientCertificates []tls.Certificate `json:"-"`
}

// WireGuardConfig holds WireGuard-specific configuration.
type WireGuardConfig struct {
	// PrivateKey is the WireGuard private key (base64 encoded).
	PrivateKey string `json:"private_key" validate:"required"`

	// PeerPublicKey is the peer's public key (base64 encoded).
	PeerPublicKey string `json:"peer_public_key" validate:"required"`

	// PreSharedKey is the optional pre-shared key (base64 encoded).
	PreSharedKey string `json:"pre_shared_key,omitempty"`

	// Endpoint is the peer's endpoint (host:port).
	Endpoint string `json:"endpoint" validate:"required"`

	// KeepAlive is the keepalive interval.
	KeepAlive time.Duration `json:"keep_alive"`

	// MTU is the MTU for the WireGuard interface.
	MTU int `json:"mtu"`

	// TunDeviceName is the name of the TUN device (e.g., "wg0").
	TunDeviceName string `json:"tun_device_name"`
}

// DefaultClientConfig returns a configuration with sensible defaults.
func DefaultClientConfig() *ClientConfig {
	return &ClientConfig{
		Transport:        TransportDTLS,
		VerifySSL:        true,
		DialTimeout:      10 * time.Second,
		HandshakeTimeout: 5 * time.Second,
		DTLSConfig: &DTLSConfig{
			CipherSuites: []uint16{
				dtls.TLS_CHACHA20_POLY1305_SHA256,
				dtls.TLS_AES_256_GCM_SHA384,
				dtls.TLS_AES_128_GCM_SHA256,
			},
		},
		WireGuardConfig: &WireGuardConfig{
			KeepAlive: 25 * time.Second,
			MTU:       1420,
		},
	}
}

// Client implements the WDTT client with DTLS and WireGuard transport.
type Client struct {
	mu           sync.RWMutex
	config       *ClientConfig
	logger       zerolog.Logger
	started      bool
	dtlsConn     *dtls.Conn
	wgDevice     *device.Device
	wgTun        tun.Device
	wgConn       net.Conn
	handshakeMu  sync.Mutex
	panelSession *PanelSession
}

// PanelSession represents a session with the WDTT panel.
type PanelSession struct {
	Token     string
	ExpiresAt time.Time
	PeerInfo  *PeerInfo
}

// PeerInfo contains information about the peer from the panel.
type PeerInfo struct {
	PublicKey   string
	Endpoint    string
	AllowedIPs  []string
	KeepAlive   time.Duration
	MTU         int
}

// DialError represents an error during dialing.
type DialError struct {
	Op  string
	Err error
}

func (e *DialError) Error() string {
	return fmt.Sprintf("wdtt: %s: %v", e.Op, e.Err)
}

func (e *DialError) Unwrap() error {
	return e.Err
}

// NewClient creates a new WDTT client with the given configuration.
func NewClient(cfg *ClientConfig) (*Client, error) {
	if cfg == nil {
		cfg = DefaultClientConfig()
	}

	if cfg.Logger == nil {
		logger := zerolog.Nop()
		cfg.Logger = &logger
	}

	// Validate transport-specific config
	if cfg.Transport == TransportDTLS && cfg.DTLSConfig == nil {
		cfg.DTLSConfig = DefaultClientConfig().DTLSConfig
	}
	if cfg.Transport == TransportWireGuard && cfg.WireGuardConfig == nil {
		cfg.WireGuardConfig = DefaultClientConfig().WireGuardConfig
	}

	return &Client{
		config: cfg,
		logger: *cfg.Logger,
	}, nil
}

// Start initializes the client and performs the panel handshake.
func (c *Client) Start(ctx context.Context) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.started {
		return errors.New("client already started")
	}

	c.logger.Info().Str("transport", string(c.config.Transport)).Msg("starting WDTT client")

	// Perform panel handshake
	if err := c.handshake(ctx); err != nil {
		return fmt.Errorf("panel handshake failed: %w", err)
	}

	// Initialize transport
	switch c.config.Transport {
	case TransportDTLS:
		if err := c.initDTLS(ctx); err != nil {
			return fmt.Errorf("DTLS init failed: %w", err)
		}
	case TransportWireGuard:
		if err := c.initWireGuard(ctx); err != nil {
			return fmt.Errorf("WireGuard init failed: %w", err)
		}
	default:
		return fmt.Errorf("unsupported transport: %s", c.config.Transport)
	}

	c.started = true
	c.logger.Info().Msg("WDTT client started")
	return nil
}

// handshake performs the WDTT panel handshake to get peer info and auth token.
func (c *Client) handshake(ctx context.Context) error {
	c.handshakeMu.Lock()
	defer c.handshakeMu.Unlock()

	ctx, cancel := context.WithTimeout(ctx, c.config.HandshakeTimeout)
	defer cancel()

	// TODO: Implement actual panel HTTP handshake
	// For now, create a mock session
	c.panelSession = &PanelSession{
		Token:     "mock-token",
		ExpiresAt: time.Now().Add(24 * time.Hour),
		PeerInfo: &PeerInfo{
			PublicKey:  "mock-peer-public-key",
			Endpoint:   "127.0.0.1:51820",
			AllowedIPs: []string{"10.0.0.0/24"},
			KeepAlive:  25 * time.Second,
			MTU:        1420,
		},
	}

	c.logger.Info().Msg("panel handshake completed")
	return nil
}

// initDTLS initializes the DTLS connection.
func (c *Client) initDTLS(ctx context.Context) error {
	cfg := c.config.DTLSConfig
	if cfg == nil {
		return errors.New("DTLS config is nil")
	}

	dtlsConfig := &dtls.Config{
		CipherSuites:       cfg.CipherSuites,
		ServerName:         cfg.ServerName,
		InsecureSkipVerify: cfg.InsecureSkipVerify || !c.config.VerifySSL,
		RootCAs:            cfg.RootCAs,
		ClientCertificates: cfg.ClientCertificates,
		ExtendedMasterSecret: dtls.RequestExtendedMasterSecret,
	}

	// Parse panel URL to get host:port
	// For DTLS over UDP, we need to dial UDP
	// Using quic-go's DTLS support via Listen/Addr
	addr, err := net.ResolveUDPAddr("udp", c.panelHostPort())
	if err != nil {
		return fmt.Errorf("resolve panel address: %w", err)
	}

	// Create a UDP connection
	udpConn, err := net.ListenUDP("udp", nil)
	if err != nil {
		return fmt.Errorf("listen UDP: %w", err)
	}

	// Create DTLS connection
	dtlsConn, err := dtls.Client(udpConn, addr, dtlsConfig)
	if err != nil {
		udpConn.Close()
		return fmt.Errorf("DTLS client: %w", err)
	}

	// Perform DTLS handshake
	handshakeCtx, cancel := context.WithTimeout(ctx, c.config.HandshakeTimeout)
	defer cancel()

	if err := dtlsConn.HandshakeContext(handshakeCtx); err != nil {
		dtlsConn.Close()
		return fmt.Errorf("DTLS handshake: %w", err)
	}

	c.dtlsConn = dtlsConn
	c.logger.Info().Str("remote", dtlsConn.RemoteAddr().String()).Msg("DTLS connection established")
	return nil
}

// initWireGuard initializes the WireGuard device and connection.
func (c *Client) initWireGuard(ctx context.Context) error {
	wgCfg := c.config.WireGuardConfig
	if wgCfg == nil {
		return errors.New("WireGuard config is nil")
	}

	// Parse private key
	privateKey, err := decodeKey(wgCfg.PrivateKey)
	if err != nil {
		return fmt.Errorf("parse private key: %w", err)
	}

	// Parse peer public key
	peerPublicKey, err := decodeKey(wgCfg.PeerPublicKey)
	if err != nil {
		return fmt.Errorf("parse peer public key: %w", err)
	}

	// Parse pre-shared key if provided
	var preSharedKey [chacha20poly1305.KeySize]byte
	if wgCfg.PreSharedKey != "" {
		psk, err := decodeKey(wgCfg.PreSharedKey)
		if err != nil {
			return fmt.Errorf("parse pre-shared key: %w", err)
		}
		copy(preSharedKey[:], psk[:])
	}

	// Create TUN device
	tunName := wgCfg.TunDeviceName
	if tunName == "" {
		tunName = "wg0"
	}
	tunDev, err := tun.CreateTUN(tunName, wgCfg.MTU)
	if err != nil {
		return fmt.Errorf("create TUN device: %w", err)
	}

	// Create WireGuard device
	logger := device.NewLogger(
		device.LogLevelError,
		fmt.Sprintf("(%s) ", tunName),
	)
	wgDev := device.NewDevice(tunDev, conn.NewDefaultBind(), logger)

	// Start the device
	wgDev.Up()

	// Configure peer
	peerConfig := device.PeerConfig{
		PublicKey:         peerPublicKey,
		PreSharedKey:      &preSharedKey,
		KeepAliveInterval: wgCfg.KeepAlive,
		Endpoint:          parseEndpoint(wgCfg.Endpoint),
		ReplaceAllowedIPs: true,
	}
	// Add allowed IPs
	for _, ip := range c.panelSession.PeerInfo.AllowedIPs {
		_, cidr, _ := net.ParseCIDR(ip)
		if cidr != nil {
			peerConfig.AllowedIPs = append(peerConfig.AllowedIPs, cidr)
		}
	}

	wgDev.SetPrivateKey(privateKey)
	wgDev.AddPeer(peerConfig)

	// Create a connection wrapper for net.Conn interface
	c.wgDevice = wgDev
	c.wgTun = tunDev
	c.wgConn = &wireguardConn{device: wgDev, tun: tunDev}

	c.logger.Info().Str("endpoint", wgCfg.Endpoint).Msg("WireGuard device initialized")
	return nil
}

// panelHostPort extracts host:port from panel URL for DTLS.
func (c *Client) panelHostPort() string {
	// Simple extraction - in production, use proper URL parsing
	// e.g., "https://panel.example.com:443" -> "panel.example.com:443"
	url := c.config.PanelURL
	if len(url) > 8 && url[:8] == "https://" {
		url = url[8:]
	} else if len(url) > 7 && url[:7] == "http://" {
		url = url[7:]
	}
	// Default to 443 if no port
	if host, _, err := net.SplitHostPort(url); err != nil {
		return net.JoinHostPort(url, "443")
	} else {
		return net.JoinHostPort(host, "443")
	}
}

// Dial establishes a connection to the given address.
// For DTLS, this creates a new stream on the existing DTLS connection.
// For WireGuard, this uses the TUN device for routing.
func (c *Client) Dial(ctx context.Context, addr string) (net.Conn, error) {
	c.mu.RLock()
	started := c.started
	c.mu.RUnlock()

	if !started {
		return nil, errors.New("client not started")
	}

	ctx, cancel := context.WithTimeout(ctx, c.config.DialTimeout)
	defer cancel()

	switch c.config.Transport {
	case TransportDTLS:
		return c.dialDTLS(ctx, addr)
	case TransportWireGuard:
		return c.dialWireGuard(ctx, addr)
	default:
		return nil, fmt.Errorf("unsupported transport: %s", c.config.Transport)
	}
}

// dialDTLS creates a new stream on the DTLS connection.
func (c *Client) dialDTLS(ctx context.Context, addr string) (net.Conn, error) {
	if c.dtlsConn == nil {
		return nil, errors.New("DTLS connection not initialized")
	}

	// For DTLS, we can use the existing connection directly
	// or create a new stream if using QUIC-style streams
	// Here we return the DTLS connection itself wrapped for the specific address
	return &dtlsConnWrapper{
		conn: c.dtlsConn,
		addr: addr,
	}, nil
}

// dialWireGuard returns a connection that routes through the WireGuard TUN.
func (c *Client) dialWireGuard(ctx context.Context, addr string) (net.Conn, error) {
	if c.wgConn == nil {
		return nil, errors.New("WireGuard connection not initialized")
	}

	// For WireGuard, the TUN device handles routing
	// We return a connection that writes to the TUN device
	return &wireguardDialConn{
		baseConn: c.wgConn,
		addr:     addr,
	}, nil
}

// Close shuts down the client and releases all resources.
func (c *Client) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if !c.started {
		return nil
	}

	c.logger.Info().Msg("closing WDTT client")
	var errs []error

	// Close DTLS connection
	if c.dtlsConn != nil {
		if err := c.dtlsConn.Close(); err != nil {
			errs = append(errs, fmt.Errorf("close DTLS: %w", err))
		}
		c.dtlsConn = nil
	}

	// Close WireGuard device
	if c.wgDevice != nil {
		c.wgDevice.Close()
		c.wgDevice = nil
	}
	if c.wgTun != nil {
		if err := c.wgTun.Close(); err != nil {
			errs = append(errs, fmt.Errorf("close TUN: %w", err))
		}
		c.wgTun = nil
	}
	c.wgConn = nil

	c.started = false
	c.panelSession = nil

	if len(errs) > 0 {
		return fmt.Errorf("close errors: %v", errs)
	}
	return nil
}

// IsStarted returns whether the client is started.
func (c *Client) IsStarted() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.started
}

// Transport returns the transport type in use.
func (c *Client) Transport() TransportType {
	return c.config.Transport
}

// PanelSession returns the current panel session info.
func (c *Client) PanelSession() *PanelSession {
	c.handshakeMu.Lock()
	defer c.handshakeMu.Unlock()
	return c.panelSession
}

// dtlsConnWrapper wraps a DTLS connection for a specific address.
type dtlsConnWrapper struct {
	conn *dtls.Conn
	addr string
}

func (w *dtlsConnWrapper) Read(b []byte) (int, error) {
	return w.conn.Read(b)
}

func (w *dtlsConnWrapper) Write(b []byte) (int, error) {
	return w.conn.Write(b)
}

func (w *dtlsConnWrapper) Close() error {
	// Don't close the underlying DTLS connection
	return nil
}

func (w *dtlsConnWrapper) LocalAddr() net.Addr {
	return w.conn.LocalAddr()
}

func (w *dtlsConnWrapper) RemoteAddr() net.Addr {
	return w.conn.RemoteAddr()
}

func (w *dtlsConnWrapper) SetDeadline(t time.Time) error {
	return w.conn.SetDeadline(t)
}

func (w *dtlsConnWrapper) SetReadDeadline(t time.Time) error {
	return w.conn.SetReadDeadline(t)
}

func (w *dtlsConnWrapper) SetWriteDeadline(t time.Time) error {
	return w.conn.SetWriteDeadline(t)
}

// wireguardConn wraps the WireGuard device for net.Conn interface.
type wireguardConn struct {
	device *device.Device
	tun    tun.Device
}

func (w *wireguardConn) Read(b []byte) (int, error) {
	return w.tun.Read(b, 0)
}

func (w *wireguardConn) Write(b []byte) (int, error) {
	return w.tun.Write(b, 0)
}

func (w *wireguardConn) Close() error {
	w.device.Close()
	return w.tun.Close()
}

func (w *wireguardConn) LocalAddr() net.Addr {
	return &net.IPAddr{IP: net.IPv4(10, 0, 0, 1)}
}

func (w *wireguardConn) RemoteAddr() net.Addr {
	return &net.IPAddr{IP: net.IPv4(10, 0, 0, 2)}
}

func (w *wireguardConn) SetDeadline(t time.Time) error {
	return nil
}

func (w *wireguardConn) SetReadDeadline(t time.Time) error {
	return nil
}

func (w *wireguardConn) SetWriteDeadline(t time.Time) error {
	return nil
}

// wireguardDialConn wraps a WireGuard connection for dialing a specific address.
type wireguardDialConn struct {
	baseConn net.Conn
	addr     string
}

func (w *wireguardDialConn) Read(b []byte) (int, error) {
	return w.baseConn.Read(b)
}

func (w *wireguardDialConn) Write(b []byte) (int, error) {
	return w.baseConn.Write(b)
}

func (w *wireguardDialConn) Close() error {
	// Don't close the base connection
	return nil
}

func (w *wireguardDialConn) LocalAddr() net.Addr {
	return w.baseConn.LocalAddr()
}

func (w *wireguardDialConn) RemoteAddr() net.Addr {
	return &net.TCPAddr{IP: net.ParseIP(w.addr), Port: 0}
}

func (w *wireguardDialConn) SetDeadline(t time.Time) error {
	return w.baseConn.SetDeadline(t)
}

func (w *wireguardDialConn) SetReadDeadline(t time.Time) error {
	return w.baseConn.SetReadDeadline(t)
}

func (w *wireguardDialConn) SetWriteDeadline(t time.Time) error {
	return w.baseConn.SetWriteDeadline(t)
}

// decodeKey decodes a base64-encoded WireGuard key.
func decodeKey(s string) ([32]byte, error) {
	var key [32]byte
	data, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return key, fmt.Errorf("base64 decode: %w", err)
	}
	if len(data) != 32 {
		return key, fmt.Errorf("key must be 32 bytes, got %d", len(data))
	}
	copy(key[:], data)
	return key, nil
}

// parseEndpoint parses a host:port string into a net.UDPAddr.
func parseEndpoint(s string) *net.UDPAddr {
	addr, err := net.ResolveUDPAddr("udp", s)
	if err != nil {
		return nil
	}
	return addr
}

// Ensure Client implements the Outbound interface from outbound package.
var _ interface {
	Start() error
	Stop() error
	Dial(network, addr string) (net.Conn, error)
} = (*Client)(nil)

// Stop is an alias for Close for interface compatibility.
func (c *Client) Stop() error {
	return c.Close()
}

// Dial implements the outbound.Outbound interface.
func (c *Client) Dial(network, addr string) (net.Conn, error) {
	return c.Dial(context.Background(), addr)
}