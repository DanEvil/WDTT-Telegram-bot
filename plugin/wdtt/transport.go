package wdtt

import (
	"context"
	"errors"
	"net"
	"time"

	"github.com/pion/dtls/v2"
	"github.com/xtaci/wireguard-go/conn"
	"github.com/xtaci/wireguard-go/device"
	"github.com/xtaci/wireguard-go/tun"
)

// TransportType represents the underlying transport type.
type TransportType string

const (
	TransportDTLS     TransportType = "dtls"
	TransportWireGuard TransportType = "wireguard"
)

// TransportConfig holds configuration for creating transports.
type TransportConfig struct {
	Type       TransportType
	RemoteAddr string
	LocalAddr  string
	// DTLS config
	DTLSConfig *dtls.Config
	// WireGuard config
	WGPrivateKey  []byte
	WGPublicKey   []byte
	WGPreSharedKey []byte
	WGKeepalive   time.Duration
}

// Transport is a unified interface for different transport implementations.
// Both DTLSTransport and WireGuardTransport implement net.Conn.
type Transport interface {
	net.Conn
	// Type returns the transport type identifier.
	Type() TransportType
	// Dial establishes a connection to the remote address.
	Dial(ctx context.Context) error
	// Close closes the transport.
	Close() error
}

// DTLSTransport implements Transport using DTLS (via pion/dtls).
type DTLSTransport struct {
	conn       *dtls.Conn
	config     *TransportConfig
	localAddr  net.Addr
	remoteAddr net.Addr
}

// NewDTLSTransport creates a new DTLS transport.
func NewDTLSTransport(config *TransportConfig) (*DTLSTransport, error) {
	if config == nil {
		return nil, errors.New("config cannot be nil")
	}
	if config.Type != TransportDTLS {
		return nil, errors.New("invalid transport type for DTLS")
	}

	dtlsConfig := config.DTLSConfig
	if dtlsConfig == nil {
		dtlsConfig = &dtls.Config{
			InsecureSkipVerify: true, // For self-signed certs; configure properly in production
		}
	}

	return &DTLSTransport{
		config: config,
	}, nil
}

// Dial establishes a DTLS connection to the remote address.
func (t *DTLSTransport) Dial(ctx context.Context) error {
	if t.config.RemoteAddr == "" {
		return errors.New("remote address not configured")
	}

	var dialer net.Dialer
	if t.config.LocalAddr != "" {
		localAddr, err := net.ResolveUDPAddr("udp", t.config.LocalAddr)
		if err != nil {
			return err
		}
		dialer.LocalAddr = localAddr
	}

	remoteAddr, err := net.ResolveUDPAddr("udp", t.config.RemoteAddr)
	if err != nil {
		return err
	}
	t.remoteAddr = remoteAddr

	udpConn, err := dialer.DialContext(ctx, "udp", t.config.RemoteAddr)
	if err != nil {
		return err
	}

	dtlsConn, err := dtls.Client(udpConn, t.config.DTLSConfig)
	if err != nil {
		udpConn.Close()
		return err
	}

	t.conn = dtlsConn
	t.localAddr = dtlsConn.LocalAddr()

	// Perform handshake with timeout
	handshakeCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	errChan := make(chan error, 1)
	go func() {
		errChan <- dtlsConn.Handshake()
	}()

	select {
	case err := <-errChan:
		if err != nil {
			dtlsConn.Close()
			return err
		}
	case <-handshakeCtx.Done():
		dtlsConn.Close()
		return handshakeCtx.Err()
	}

	return nil
}

// Read reads data from the DTLS connection.
func (t *DTLSTransport) Read(b []byte) (int, error) {
	if t.conn == nil {
		return 0, errors.New("connection not established")
	}
	return t.conn.Read(b)
}

// Write writes data to the DTLS connection.
func (t *DTLSTransport) Write(b []byte) (int, error) {
	if t.conn == nil {
		return 0, errors.New("connection not established")
	}
	return t.conn.Write(b)
}

// Close closes the DTLS connection.
func (t *DTLSTransport) Close() error {
	if t.conn != nil {
		return t.conn.Close()
	}
	return nil
}

// LocalAddr returns the local network address.
func (t *DTLSTransport) LocalAddr() net.Addr {
	if t.conn != nil {
		return t.conn.LocalAddr()
	}
	return t.localAddr
}

// RemoteAddr returns the remote network address.
func (t *DTLSTransport) RemoteAddr() net.Addr {
	if t.conn != nil {
		return t.conn.RemoteAddr()
	}
	return t.remoteAddr
}

// SetDeadline sets the read and write deadlines.
func (t *DTLSTransport) SetDeadline(deadline time.Time) error {
	if t.conn != nil {
		return t.conn.SetDeadline(deadline)
	}
	return errors.New("connection not established")
}

// SetReadDeadline sets the read deadline.
func (t *DTLSTransport) SetReadDeadline(deadline time.Time) error {
	if t.conn != nil {
		return t.conn.SetReadDeadline(deadline)
	}
	return errors.New("connection not established")
}

// SetWriteDeadline sets the write deadline.
func (t *DTLSTransport) SetWriteDeadline(deadline time.Time) error {
	if t.conn != nil {
		return t.conn.SetWriteDeadline(deadline)
	}
	return errors.New("connection not established")
}

// Type returns the transport type.
func (t *DTLSTransport) Type() TransportType {
	return TransportDTLS
}

// WireGuardTransport implements Transport using WireGuard (via wireguard-go).
type WireGuardTransport struct {
	device     *device.Device
	tunDev     tun.Device
	bind       conn.Bind
	config     *TransportConfig
	localAddr  net.Addr
	remoteAddr net.Addr
}

// NewWireGuardTransport creates a new WireGuard transport.
func NewWireGuardTransport(config *TransportConfig) (*WireGuardTransport, error) {
	if config == nil {
		return nil, errors.New("config cannot be nil")
	}
	if config.Type != TransportWireGuard {
		return nil, errors.New("invalid transport type for WireGuard")
	}
	if len(config.WGPrivateKey) == 0 {
		return nil, errors.New("WireGuard private key required")
	}
	if len(config.WGPublicKey) == 0 {
		return nil, errors.New("WireGuard peer public key required")
	}

	return &WireGuardTransport{
		config: config,
	}, nil
}

// Dial establishes a WireGuard connection.
func (t *WireGuardTransport) Dial(ctx context.Context) error {
	if t.config.RemoteAddr == "" {
		return errors.New("remote address not configured")
	}

	remoteAddr, err := net.ResolveUDPAddr("udp", t.config.RemoteAddr)
	if err != nil {
		return err
	}
	t.remoteAddr = remoteAddr

	// Create a UDP bind for WireGuard
	localAddr := &net.UDPAddr{IP: net.IPv4zero, Port: 0}
	if t.config.LocalAddr != "" {
		localAddr, err = net.ResolveUDPAddr("udp", t.config.LocalAddr)
		if err != nil {
			return err
		}
	}

	udpConn, err := net.ListenUDP("udp", localAddr)
	if err != nil {
		return err
	}
	t.localAddr = udpConn.LocalAddr()

	// Create WireGuard bind
	t.bind = conn.NewDefaultBind(udpConn)

	// Create TUN device
	tunDev, err := tun.CreateTUN("", 1420)
	if err != nil {
		udpConn.Close()
		return err
	}
	t.tunDev = tunDev

	// Create WireGuard device
	t.device = device.NewDevice(t.tunDev, t.bind, device.NewLogger(device.LogLevelSilent, "wireguard: "))

	// Configure peer
	peerConfig := device.PeerConfig{
		PublicKey:  configToKey(t.config.WGPublicKey),
		PreSharedKey: configToKey(t.config.WGPreSharedKey),
		Endpoint:   remoteAddr,
		Keepalive:  t.config.WGKeepalive,
	}

	if err := t.device.SetPrivateKey(configToKey(t.config.WGPrivateKey)); err != nil {
		t.Close()
		return err
	}

	if _, err := t.device.AddPeer(peerConfig); err != nil {
		t.Close()
		return err
	}

	// Bring device up
	t.device.Up()

	return nil
}

// configToKey converts byte slice to device.NoisePublicKey/PrivateKey.
func configToKey(b []byte) (key [32]byte) {
	copy(key[:], b)
	return
}

// Read reads data from the WireGuard TUN device.
func (t *WireGuardTransport) Read(b []byte) (int, error) {
	if t.tunDev == nil {
		return 0, errors.New("device not initialized")
	}
	return t.tunDev.Read([][]byte{b}, 0)
}

// Write writes data to the WireGuard TUN device.
func (t *WireGuardTransport) Write(b []byte) (int, error) {
	if t.tunDev == nil {
		return 0, errors.New("device not initialized")
	}
	_, err := t.tunDev.Write([][]byte{b}, 0)
	if err != nil {
		return 0, err
	}
	return len(b), nil
}

// Close closes the WireGuard device and TUN.
func (t *WireGuardTransport) Close() error {
	var errs []error
	if t.device != nil {
		t.device.Close()
	}
	if t.tunDev != nil {
		if err := t.tunDev.Close(); err != nil {
			errs = append(errs, err)
		}
	}
	if len(errs) > 0 {
		return errors.Join(errs...)
	}
	return nil
}

// LocalAddr returns the local network address.
func (t *WireGuardTransport) LocalAddr() net.Addr {
	return t.localAddr
}

// RemoteAddr returns the remote network address.
func (t *WireGuardTransport) RemoteAddr() net.Addr {
	return t.remoteAddr
}

// SetDeadline sets the read and write deadlines.
// Note: WireGuard doesn't support deadlines directly on the TUN device.
func (t *WireGuardTransport) SetDeadline(deadline time.Time) error {
	return errors.New("deadline not supported on WireGuard transport")
}

// SetReadDeadline sets the read deadline.
func (t *WireGuardTransport) SetReadDeadline(deadline time.Time) error {
	return errors.New("deadline not supported on WireGuard transport")
}

// SetWriteDeadline sets the write deadline.
func (t *WireGuardTransport) SetWriteDeadline(deadline time.Time) error {
	return errors.New("deadline not supported on WireGuard transport")
}

// Type returns the transport type.
func (t *WireGuardTransport) Type() TransportType {
	return TransportWireGuard
}

// NewTransport creates a new transport based on the configuration.
func NewTransport(config *TransportConfig) (Transport, error) {
	if config == nil {
		return nil, errors.New("config cannot be nil")
	}

	switch config.Type {
	case TransportDTLS:
		return NewDTLSTransport(config)
	case TransportWireGuard:
		return NewWireGuardTransport(config)
	default:
		return nil, errors.New("unsupported transport type: " + string(config.Type))
	}
}