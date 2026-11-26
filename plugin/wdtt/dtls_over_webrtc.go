package wdtt

import (
	"context"
	"errors"
	"net"
	"sync"
	"time"

	"github.com/pion/dtls/v2"
	"github.com/pion/webrtc/v3"
)

// DataChannelPacketConn implements net.PacketConn over a WebRTC DataChannel.
// It buffers incoming DTLS packets and provides packet-oriented semantics
// required by pion/dtls.
type DataChannelPacketConn struct {
	dc          *webrtc.DataChannel
	readBuf     [][]byte
	readBufMu   sync.Mutex
	readCond    *sync.Cond
	localAddr   net.Addr
	remoteAddr  net.Addr
	closed      bool
	closeMu     sync.Mutex
	readDeadline  time.Time
	writeDeadline time.Time
	deadlineMu    sync.Mutex
}

var _ net.PacketConn = (*DataChannelPacketConn)(nil)

// NewDataChannelPacketConn creates a new packet connection wrapper around a DataChannel.
func NewDataChannelPacketConn(dc *webrtc.DataChannel) *DataChannelPacketConn {
	pc := &DataChannelPacketConn{
		dc:         dc,
		readBuf:    make([][]byte, 0),
		localAddr:  &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		remoteAddr: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
	}
	pc.readCond = sync.NewCond(&pc.readBufMu)

	// Register message handler
	dc.OnMessage(func(msg webrtc.DataChannelMessage) {
		pc.readBufMu.Lock()
		defer pc.readBufMu.Unlock()

		// Copy the data since the buffer may be reused
		data := make([]byte, len(msg.Data))
		copy(data, msg.Data)
		pc.readBuf = append(pc.readBuf, data)
		pc.readCond.Signal()
	})

	return pc
}

// ReadFrom reads a packet from the data channel.
func (pc *DataChannelPacketConn) ReadFrom(b []byte) (int, net.Addr, error) {
	pc.readBufMu.Lock()
	defer pc.readBufMu.Unlock()

	for {
		// Check deadline
		if !pc.readDeadline.IsZero() && time.Now().After(pc.readDeadline) {
			return 0, nil, &net.OpError{Op: "read", Net: "webrtc", Err: errors.New("i/o timeout")}
		}

		if len(pc.readBuf) > 0 {
			pkt := pc.readBuf[0]
			pc.readBuf = pc.readBuf[1:]
			n := copy(b, pkt)
			return n, pc.remoteAddr, nil
		}

		if pc.closed {
			return 0, nil, &net.OpError{Op: "read", Net: "webrtc", Err: errors.New("connection closed")}
		}

		// Wait for data or deadline
		if !pc.readDeadline.IsZero() {
			waitTime := time.Until(pc.readDeadline)
			if waitTime <= 0 {
				return 0, nil, &net.OpError{Op: "read", Net: "webrtc", Err: errors.New("i/o timeout")}
			}
			// Use a timer to implement deadline-aware wait
			done := make(chan struct{})
			go func() {
				pc.readCond.Wait()
				close(done)
			}()
			select {
			case <-done:
				continue
			case <-time.After(waitTime):
				return 0, nil, &net.OpError{Op: "read", Net: "webrtc", Err: errors.New("i/o timeout")}
			}
		}

		pc.readCond.Wait()
	}
}

// WriteTo writes a packet to the data channel.
func (pc *DataChannelPacketConn) WriteTo(b []byte, addr net.Addr) (int, error) {
	pc.closeMu.Lock()
	if pc.closed {
		pc.closeMu.Unlock()
		return 0, &net.OpError{Op: "write", Net: "webrtc", Err: errors.New("connection closed")}
	}
	pc.closeMu.Unlock()

	// Check write deadline
	pc.deadlineMu.Lock()
	writeDeadline := pc.writeDeadline
	pc.deadlineMu.Unlock()

	if !writeDeadline.IsZero() && time.Now().After(writeDeadline) {
		return 0, &net.OpError{Op: "write", Net: "webrtc", Err: errors.New("i/o timeout")}
	}

	// Send data through data channel
	// DataChannel.Send blocks until the data is sent or the channel is closed
	if err := pc.dc.Send(b); err != nil {
		return 0, &net.OpError{Op: "write", Net: "webrtc", Err: err}
	}

	return len(b), nil
}

// Close closes the data channel.
func (pc *DataChannelPacketConn) Close() error {
	pc.closeMu.Lock()
	defer pc.closeMu.Unlock()

	if pc.closed {
		return nil
	}
	pc.closed = true

	// Unblock any waiting readers
	pc.readBufMu.Lock()
	pc.readCond.Broadcast()
	pc.readBufMu.Unlock()

	return pc.dc.Close()
}

// LocalAddr returns the local network address.
func (pc *DataChannelPacketConn) LocalAddr() net.Addr {
	return pc.localAddr
}

// SetDeadline sets the read and write deadlines.
func (pc *DataChannelPacketConn) SetDeadline(t time.Time) error {
	pc.deadlineMu.Lock()
	defer pc.deadlineMu.Unlock()
	pc.readDeadline = t
	pc.writeDeadline = t
	return nil
}

// SetReadDeadline sets the read deadline.
func (pc *DataChannelPacketConn) SetReadDeadline(t time.Time) error {
	pc.deadlineMu.Lock()
	defer pc.deadlineMu.Unlock()
	pc.readDeadline = t
	// Unblock any waiting read
	pc.readBufMu.Lock()
	pc.readCond.Broadcast()
	pc.readBufMu.Unlock()
	return nil
}

// SetWriteDeadline sets the write deadline.
func (pc *DataChannelPacketConn) SetWriteDeadline(t time.Time) error {
	pc.deadlineMu.Lock()
	defer pc.deadlineMu.Unlock()
	pc.writeDeadline = t
	return nil
}

// DTLSOverWebRTCTransport implements the Transport interface using DTLS over WebRTC DataChannels.
type DTLSOverWebRTCTransport struct {
	config       *dtls.Config
	dialTimeout  time.Duration
	mu           sync.Mutex
	conns        map[net.Conn]struct{}
	closed       bool
	peerConn     *webrtc.PeerConnection // Reference to peer connection for lifecycle
}

// NewDTLSOverWebRTCTransport creates a new DTLS-over-WebRTC transport.
func NewDTLSOverWebRTCTransport(config *dtls.Config, dialTimeout time.Duration) *DTLSOverWebRTCTransport {
	if config == nil {
		config = &dtls.Config{
			InsecureSkipVerify: true, // For testing; should be configured properly in production
		}
	}
	if dialTimeout <= 0 {
		dialTimeout = 10 * time.Second
	}
	return &DTLSOverWebRTCTransport{
		config:      config,
		dialTimeout: dialTimeout,
		conns:       make(map[net.Conn]struct{}),
	}
}

// SetPeerConnection sets the peer connection for lifecycle management.
func (t *DTLSOverWebRTCTransport) SetPeerConnection(pc *webrtc.PeerConnection) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.peerConn = pc
}

// createDTLSOverDataChannel creates a DTLS connection over a WebRTC DataChannel.
// This is the main factory function for creating net.Conn from a data channel.
func createDTLSOverDataChannel(channel *webrtc.DataChannel, config *dtls.Config, isClient bool) (net.Conn, error) {
	if config == nil {
		config = &dtls.Config{
			InsecureSkipVerify: true,
		}
	}

	// Create packet connection wrapper
	packetConn := NewDataChannelPacketConn(channel)

	// Create DTLS connection
	var dtlsConn *dtls.Conn
	var err error

	if isClient {
		dtlsConn, err = dtls.Client(packetConn, config)
	} else {
		dtlsConn, err = dtls.Server(packetConn, config)
	}

	if err != nil {
		packetConn.Close()
		return nil, err
	}

	// Perform handshake with timeout
	handshakeCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := dtlsConn.HandshakeContext(handshakeCtx); err != nil {
		dtlsConn.Close()
		packetConn.Close()
		return nil, err
	}

	return dtlsConn, nil
}

// Dial establishes a DTLS connection over a new WebRTC DataChannel.
// The addr parameter is used to identify the target (e.g., for logging).
// In practice, the data channel should be created via WebRTC signaling beforehand.
func (t *DTLSOverWebRTCTransport) Dial(ctx context.Context, addr string) (net.Conn, error) {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return nil, errors.New("transport is closed")
	}
	// We need a peer connection to create data channels
	if t.peerConn == nil {
		t.mu.Unlock()
		return nil, errors.New("peer connection not set; call SetPeerConnection first")
	}
	peerConn := t.peerConn
	t.mu.Unlock()

	dialCtx, cancel := context.WithTimeout(ctx, t.dialTimeout)
	defer cancel()

	// Create a new data channel for this connection
	// Use reliable, ordered data channel for DTLS (which handles its own reliability)
	dc, err := peerConn.CreateDataChannel("dtls-"+addr, &webrtc.DataChannelInit{
		Ordered:        boolPtr(true),
		MaxRetransmits: nil, // Reliable mode
	})
	if err != nil {
		return nil, err
	}

	// Wait for data channel to open
	openCtx, openCancel := context.WithTimeout(dialCtx, 5*time.Second)
	defer openCancel()

	openCh := make(chan error, 1)
	dc.OnOpen(func() {
		select {
		case openCh <- nil:
		default:
		}
	})
	dc.OnError(func(err error) {
		select {
		case openCh <- err:
		default:
		}
	})

	select {
	case err := <-openCh:
		if err != nil {
			dc.Close()
			return nil, err
		}
	case <-openCtx.Done():
		dc.Close()
		return nil, openCtx.Err()
	}

	// Create DTLS connection over the data channel
	dtlsConn, err := createDTLSOverDataChannel(dc, t.config, true)
	if err != nil {
		dc.Close()
		return nil, err
	}

	wrapped := &dtlsWebRTCConnWrapper{Conn: dtlsConn, dc: dc, transport: t}
	t.trackConn(wrapped)

	return wrapped, nil
}

// Close closes the transport and all tracked connections.
func (t *DTLSOverWebRTCTransport) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()

	if t.closed {
		return errors.New("transport already closed")
	}
	t.closed = true

	var firstErr error
	for conn := range t.conns {
		if err := conn.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	t.conns = nil
	return firstErr
}

// Type returns the transport type.
func (t *DTLSOverWebRTCTransport) Type() TransportType {
	return TransportType("dtls-webrtc")
}

func (t *DTLSOverWebRTCTransport) trackConn(conn *dtlsWebRTCConnWrapper) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.closed {
		t.conns[conn] = struct{}{}
	}
}

// dtlsWebRTCConnWrapper wraps a DTLS connection with its associated DataChannel
// to ensure proper cleanup when closed.
type dtlsWebRTCConnWrapper struct {
	net.Conn
	dc       *webrtc.DataChannel
	transport *DTLSOverWebRTCTransport
	closeOnce sync.Once
}

func (w *dtlsWebRTCConnWrapper) Close() error {
	var err error
	w.closeOnce.Do(func() {
		// Close DTLS connection first
		if w.Conn != nil {
			err = w.Conn.Close()
		}
		// Then close the data channel
		if w.dc != nil {
			if dcErr := w.dc.Close(); dcErr != nil && err == nil {
				err = dcErr
			}
		}
	})
	return err
}

// AcceptDTLSOverDataChannel accepts an incoming DTLS connection over a WebRTC DataChannel.
// This should be called when a new data channel is received from the remote peer.
func AcceptDTLSOverDataChannel(channel *webrtc.DataChannel, config *dtls.Config) (net.Conn, error) {
	return createDTLSOverDataChannel(channel, config, false)
}

// boolPtr returns a pointer to a bool value (helper for DataChannelInit).
func boolPtr(b bool) *bool {
	return &b
}

// DTLSWebRTCDialer is a dialer function compatible with the Pool dialer interface.
// It creates DTLS connections over WebRTC data channels using a pre-established peer connection.
type DTLSWebRTCDialer struct {
	peerConn    *webrtc.PeerConnection
	dtlsConfig  *dtls.Config
	dialTimeout time.Duration
}

// NewDTLSWebRTCDialer creates a new dialer for DTLS over WebRTC.
func NewDTLSWebRTCDialer(peerConn *webrtc.PeerConnection, dtlsConfig *dtls.Config, dialTimeout time.Duration) *DTLSWebRTCDialer {
	if dtlsConfig == nil {
		dtlsConfig = &dtls.Config{InsecureSkipVerify: true}
	}
	if dialTimeout <= 0 {
		dialTimeout = 10 * time.Second
	}
	return &DTLSWebRTCDialer{
		peerConn:    peerConn,
		dtlsConfig:  dtlsConfig,
		dialTimeout: dialTimeout,
	}
}

// Dial implements the dialer function signature for the connection pool.
func (d *DTLSWebRTCDialer) Dial(ctx context.Context, creds Credentials) (net.Conn, error) {
	dialCtx, cancel := context.WithTimeout(ctx, d.dialTimeout)
	defer cancel()

	// Create a data channel for this connection
	dc, err := d.peerConn.CreateDataChannel("dtls-pool-"+creds.UserID, &webrtc.DataChannelInit{
		Ordered:        boolPtr(true),
		MaxRetransmits: nil,
	})
	if err != nil {
		return nil, err
	}

	// Wait for open
	openCh := make(chan error, 1)
	dc.OnOpen(func() {
		select {
		case openCh <- nil:
		default:
		}
	})
	dc.OnError(func(err error) {
		select {
		case openCh <- err:
		default:
		}
	})

	select {
	case err := <-openCh:
		if err != nil {
			dc.Close()
			return nil, err
		}
	case <-dialCtx.Done():
		dc.Close()
		return nil, dialCtx.Err()
	}

	return createDTLSOverDataChannel(dc, d.dtlsConfig, true)
}

// HandleIncomingDataChannel handles an incoming data channel from the remote peer,
// performing DTLS handshake and returning a net.Conn.
func HandleIncomingDataChannel(channel *webrtc.DataChannel, dtlsConfig *dtls.Config) (net.Conn, error) {
	if dtlsConfig == nil {
		dtlsConfig = &dtls.Config{InsecureSkipVerify: true}
	}
	return AcceptDTLSOverDataChannel(channel, dtlsConfig)
}